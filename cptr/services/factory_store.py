"""Transactional persistence for the durable Dark Factory core domain."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import and_, func, or_, select, update
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
        raise FactoryPayloadTooLarge(f"factory payload exceeds {_MAX_FACTORY_PAYLOAD_BYTES} bytes")
    return safe, hashlib.sha256(encoded).hexdigest()


def _config_fingerprint(
    *, policy: dict[str, Any], budget: dict[str, Any], model_id: str | None
) -> str:
    _, digest = _canonical_payload({"policy": policy, "budget": budget, "model_id": model_id or ""})
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
                # The immutable run.created event has a foreign key to this
                # newly-created run. Flush the parent first so SQLite FK
                # enforcement cannot observe the event before its run row.
                await db.flush()
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
                # cycle.created references the new cycle via FactoryEvent.cycle_id.
                # Persist the parent before appending the immutable event.
                await db.flush()
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

    async def advance_cycle(
        self,
        run_id: str,
        cycle_id: str,
        *,
        reason: str,
        evidence_ids: Sequence[str],
        idempotency_key: str,
    ) -> FactoryCycle:
        """Atomically close one completed cycle and enter AUDITING on the next cycle."""
        if not idempotency_key.strip():
            raise ValueError("cycle advance idempotency key must not be blank")
        intent_payload = {
            "previous_cycle_id": cycle_id,
            "to_state": FactoryState.AUDITING.value,
            "reason": reason,
            "evidence_ids": list(evidence_ids),
        }
        _, intent_digest = _canonical_payload(intent_payload)
        async with self._session_factory() as db:
            async with db.begin():
                run = await db.get(FactoryRun, run_id)
                previous_cycle = await db.get(FactoryCycle, cycle_id)
                if run is None or previous_cycle is None or previous_cycle.run_id != run_id:
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
                    if existing.payload_digest != intent_digest:
                        raise FactoryIdempotencyConflict(
                            "cycle advance idempotency key was replayed with different intent"
                        )
                    next_cycle_id = str((existing.payload or {}).get("next_cycle_id") or "")
                    next_cycle = (
                        await db.get(FactoryCycle, next_cycle_id) if next_cycle_id else None
                    )
                    if next_cycle is None:
                        raise RuntimeError(
                            "durable cycle advance event references a missing next cycle"
                        )
                    return next_cycle
                if run.current_cycle_id != cycle_id:
                    raise ValueError("cycle advance requires the active completed factory cycle")
                current = FactoryState(run.state)
                if current is not FactoryState.CYCLE_COMPLETE:
                    raise ValueError("cycle advance requires CYCLE_COMPLETE state")
                validate_factory_transition(
                    current,
                    FactoryState.AUDITING,
                    FactoryActor.SYSTEM,
                    machine_victory=False,
                )
                last_ordinal = (
                    await db.execute(
                        select(func.max(FactoryCycle.ordinal)).where(FactoryCycle.run_id == run_id)
                    )
                ).scalar_one()
                now = _now_ms()
                next_cycle = FactoryCycle(
                    id=_new_id("cycle"),
                    run_id=run_id,
                    ordinal=int(last_ordinal or 0) + 1,
                    state=FactoryState.AUDITING.value,
                    idempotency_key=f"advance:{idempotency_key}",
                    base_revision=previous_cycle.target_revision,
                    base_fingerprint=previous_cycle.target_fingerprint,
                    created_at=now,
                    updated_at=now,
                )
                db.add(next_cycle)
                await db.flush()
                previous_cycle.completed_at = previous_cycle.completed_at or now
                previous_cycle.updated_at = now
                run.current_cycle_id = next_cycle.id
                run.state = FactoryState.AUDITING.value
                run.resumable_state = None
                run.updated_at = now
                event_payload = dict(intent_payload)
                event_payload["next_cycle_id"] = next_cycle.id
                event_payload["ordinal"] = next_cycle.ordinal
                event_payload["base_revision"] = next_cycle.base_revision
                event_payload["base_fingerprint"] = next_cycle.base_fingerprint
                await self._append_event_in_transaction(
                    db,
                    run=run,
                    cycle_id=next_cycle.id,
                    actor=FactoryActor.SYSTEM,
                    event_type="state.transition",
                    from_state=FactoryState.CYCLE_COMPLETE,
                    to_state=FactoryState.AUDITING,
                    idempotency_key=idempotency_key,
                    payload=event_payload,
                    payload_digest=intent_digest,
                )
            return next_cycle

    async def list_cycles(self, run_id: str) -> list[FactoryCycle]:
        async with self._session_factory() as db:
            rows = await db.execute(
                select(FactoryCycle)
                .where(FactoryCycle.run_id == run_id)
                .order_by(FactoryCycle.ordinal)
            )
            return list(rows.scalars().all())

    async def get_cycle(self, cycle_id: str) -> FactoryCycle | None:
        async with self._session_factory() as db:
            return await db.get(FactoryCycle, cycle_id)

    async def update_cycle_projection(
        self,
        run_id: str,
        cycle_id: str,
        *,
        updates: dict[str, Any],
        run_next_action: str | None,
        idempotency_key: str,
    ) -> FactoryCycle:
        allowed = {
            "selected_finding",
            "capability_requirements",
            "selected_capabilities",
            "gate_plan",
            "base_revision",
            "base_fingerprint",
            "mutation_worker_id",
            "next_action",
        }
        unknown = sorted(set(updates) - allowed)
        if unknown:
            raise ValueError(f"unsupported factory cycle projection field {unknown[0]}")
        payload = {
            "cycle_id": cycle_id,
            "updates": updates,
            "run_next_action": run_next_action,
        }
        safe_payload, digest = _canonical_payload(payload)
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
                            "phase projection idempotency key was replayed with different intent"
                        )
                    return cycle
                now = _now_ms()
                safe_updates = safe_payload["updates"]
                for key, value in safe_updates.items():
                    setattr(cycle, key, value)
                if run_next_action is not None:
                    next_action = str(run_next_action)[:4_000]
                    run.next_action = next_action
                    cycle.next_action = next_action
                cycle.updated_at = now
                run.updated_at = now
                await self._append_event_in_transaction(
                    db,
                    run=run,
                    cycle_id=cycle_id,
                    actor=FactoryActor.SYSTEM,
                    event_type="phase.projection_updated",
                    from_state=FactoryState(run.state),
                    to_state=FactoryState(run.state),
                    idempotency_key=idempotency_key,
                    payload=safe_payload,
                    payload_digest=digest,
                )
            return cycle

    async def record_failure(
        self,
        run_id: str,
        cycle_id: str,
        *,
        signature: str,
        category: str,
        code: str,
        gate_id: str | None,
        summary: str,
        idempotency_key: str,
    ) -> FactoryCycle:
        if not signature.strip() or not category.strip() or not code.strip():
            raise ValueError("factory failure identity fields must not be blank")
        payload = {
            "cycle_id": cycle_id,
            "signature": signature,
            "category": category,
            "code": code,
            "gate_id": gate_id,
            "summary": summary[:4_000],
        }
        safe_payload, digest = _canonical_payload(payload)
        async with self._session_factory() as db:
            async with db.begin():
                run = await db.get(FactoryRun, run_id)
                cycle = await db.get(FactoryCycle, cycle_id)
                if run is None or cycle is None or cycle.run_id != run_id:
                    raise KeyError("factory run/cycle not found")
                existing_event = (
                    await db.execute(
                        select(FactoryEvent).where(
                            FactoryEvent.run_id == run_id,
                            FactoryEvent.idempotency_key == idempotency_key,
                        )
                    )
                ).scalar_one_or_none()
                if existing_event is not None:
                    if existing_event.payload_digest != digest:
                        raise FactoryIdempotencyConflict(
                            "factory failure idempotency key was replayed with different evidence"
                        )
                    return cycle
                now = _now_ms()
                signatures = dict(cycle.failure_signatures or {})
                previous = dict(signatures.get(signature) or {})
                count = int(previous.get("count") or 0) + 1
                signatures[signature] = {
                    "signature": signature,
                    "category": category,
                    "code": code,
                    "gate_id": gate_id,
                    "count": count,
                    "first_seen_at": int(previous.get("first_seen_at") or now),
                    "last_seen_at": now,
                    "last_summary": safe_payload["summary"],
                }
                cycle.failure_signatures = signatures
                cycle.attempt_count = int(cycle.attempt_count or 0) + 1
                cycle.next_action = "diagnose persisted failure before retry"
                cycle.updated_at = now
                run.next_action = cycle.next_action
                run.updated_at = now
                await self._append_event_in_transaction(
                    db,
                    run=run,
                    cycle_id=cycle_id,
                    actor=FactoryActor.SYSTEM,
                    event_type="failure.recorded",
                    from_state=FactoryState(run.state),
                    to_state=FactoryState(run.state),
                    idempotency_key=idempotency_key,
                    payload=safe_payload,
                    payload_digest=digest,
                )
            return cycle

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
                if current in {
                    FactoryState.RECOVERING,
                    FactoryState.PAUSED,
                    FactoryState.APPROVAL_REQUIRED,
                }:
                    effective_resumable = (
                        FactoryState(run.resumable_state)
                        if run.resumable_state
                        else resumable_state
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
                if to_state is FactoryState.RECOVERING:
                    if current is FactoryState.MISSION:
                        run.resumable_state = None
                    else:
                        if resumable_state is not None and resumable_state is not current:
                            raise ValueError(
                                "recovery resumable state must match the interrupted state"
                            )
                        run.resumable_state = current.value
                elif to_state in {FactoryState.PAUSED, FactoryState.APPROVAL_REQUIRED}:
                    run.resumable_state = current.value
                elif current in {
                    FactoryState.RECOVERING,
                    FactoryState.PAUSED,
                    FactoryState.APPROVAL_REQUIRED,
                }:
                    run.resumable_state = None
                run.state = to_state.value
                run.updated_at = now
                cycle = None
                if run.current_cycle_id:
                    cycle = await db.get(FactoryCycle, run.current_cycle_id)
                    if cycle is not None:
                        cycle.state = to_state.value
                        cycle.updated_at = now
                if to_state is FactoryState.BLOCKED:
                    next_action = reason[:4_000]
                    run.next_action = next_action
                    if cycle is not None:
                        cycle.next_action = next_action
                elif to_state not in {
                    FactoryState.PAUSED,
                    FactoryState.APPROVAL_REQUIRED,
                    FactoryState.REPAIR_REQUIRED,
                }:
                    run.next_action = None
                    if cycle is not None:
                        cycle.next_action = None
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
            raise TypeError(
                "Victory authorization requires a machine-issued FactoryVictoryDecision"
            )
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
        idempotency_key: str | None = None,
    ) -> FactoryEvidence:
        if idempotency_key is not None and not idempotency_key.strip():
            raise ValueError("evidence idempotency key must not be blank")
        safe_payload, digest = _canonical_payload(payload)
        async with self._session_factory() as db:
            if idempotency_key is not None:
                existing = (
                    await db.execute(
                        select(FactoryEvidence).where(
                            FactoryEvidence.run_id == run_id,
                            FactoryEvidence.idempotency_key == idempotency_key,
                        )
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    identity = (
                        existing.cycle_id,
                        existing.gate_id,
                        existing.kind,
                        existing.source,
                        existing.authority,
                        existing.revision,
                        existing.fingerprint,
                        existing.digest,
                    )
                    requested = (
                        cycle_id,
                        gate_id,
                        kind,
                        source,
                        authority.value,
                        revision,
                        fingerprint,
                        digest,
                    )
                    if identity != requested:
                        raise FactoryIdempotencyConflict(
                            "evidence idempotency key was replayed with different evidence"
                        )
                    return existing
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
                idempotency_key=idempotency_key,
                created_at=_now_ms(),
            )
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

    async def list_evidence_page(
        self,
        run_id: str,
        *,
        after_id: str | None = None,
        limit: int = 100,
    ) -> list[FactoryEvidence]:
        limit = max(1, min(int(limit), 500))
        async with self._session_factory() as db:
            query = select(FactoryEvidence).where(FactoryEvidence.run_id == run_id)
            if after_id:
                cursor = await db.get(FactoryEvidence, after_id)
                if cursor is None or cursor.run_id != run_id:
                    raise ValueError("invalid factory evidence cursor")
                query = query.where(
                    or_(
                        FactoryEvidence.created_at > cursor.created_at,
                        and_(
                            FactoryEvidence.created_at == cursor.created_at,
                            FactoryEvidence.id > cursor.id,
                        ),
                    )
                )
            rows = await db.execute(
                query.order_by(FactoryEvidence.created_at, FactoryEvidence.id).limit(limit)
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
        idempotency_key: str | None = None,
    ) -> FactoryGateResult:
        if idempotency_key is not None and not idempotency_key.strip():
            raise ValueError("gate idempotency key must not be blank")
        now = _now_ms()
        async with self._session_factory() as db:
            if idempotency_key is not None:
                existing = (
                    await db.execute(
                        select(FactoryGateResult).where(
                            FactoryGateResult.run_id == run_id,
                            FactoryGateResult.idempotency_key == idempotency_key,
                        )
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    identity = (
                        existing.cycle_id,
                        existing.gate_id,
                        existing.category,
                        bool(existing.required),
                        bool(existing.applicable),
                        existing.status,
                        list(existing.evidence_ids or []),
                        existing.evaluated_revision,
                        existing.evaluated_fingerprint,
                        existing.reason,
                        int(existing.attempt),
                    )
                    requested = (
                        cycle_id,
                        gate_id,
                        category,
                        bool(required),
                        bool(applicable),
                        status,
                        list(evidence_ids),
                        evaluated_revision,
                        evaluated_fingerprint,
                        reason,
                        int(attempt),
                    )
                    if identity != requested:
                        raise FactoryIdempotencyConflict(
                            "gate idempotency key was replayed with different gate evidence"
                        )
                    return existing
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
                idempotency_key=idempotency_key,
                created_at=now,
                updated_at=now,
            )
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

    async def append_user_event(
        self,
        *,
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
        idempotency_key: str | None,
        cycle_id: str | None = None,
    ) -> FactoryEvent:
        if event_type not in {"user.message", "approval.decision"}:
            raise ValueError("unsupported user factory event type")
        safe_payload, digest = _canonical_payload(payload)
        async with self._session_factory() as db:
            async with db.begin():
                run = await db.get(FactoryRun, run_id)
                if run is None:
                    raise KeyError("factory run not found")
                if cycle_id is not None:
                    cycle = await db.get(FactoryCycle, cycle_id)
                    if cycle is None or cycle.run_id != run_id:
                        raise KeyError("factory run/cycle not found")
                if idempotency_key:
                    existing = (
                        await db.execute(
                            select(FactoryEvent).where(
                                FactoryEvent.run_id == run_id,
                                FactoryEvent.idempotency_key == idempotency_key,
                            )
                        )
                    ).scalar_one_or_none()
                    if existing is not None:
                        if existing.event_type != event_type or existing.payload_digest != digest:
                            raise FactoryIdempotencyConflict(
                                "factory user event idempotency key was replayed with different intent"
                            )
                        return existing
                return await self._append_event_in_transaction(
                    db,
                    run=run,
                    cycle_id=cycle_id if cycle_id is not None else run.current_cycle_id,
                    actor=FactoryActor.USER,
                    event_type=event_type,
                    from_state=FactoryState(run.state),
                    to_state=FactoryState(run.state),
                    idempotency_key=idempotency_key,
                    payload=safe_payload,
                    payload_digest=digest,
                )

    async def latest_state_entry(
        self,
        run_id: str,
        *,
        cycle_id: str,
        state: FactoryState,
    ) -> FactoryEvent | None:
        async with self._session_factory() as db:
            return (
                await db.execute(
                    select(FactoryEvent)
                    .where(
                        FactoryEvent.run_id == run_id,
                        FactoryEvent.cycle_id == cycle_id,
                        FactoryEvent.to_state == state.value,
                        FactoryEvent.event_type.in_(
                            ("state.transition", "victory.authorized", "cycle.created")
                        ),
                    )
                    .order_by(FactoryEvent.sequence.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()

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
