"""One-state-per-lease durable Dark Factory orchestrator.

The orchestrator never contains one giant factory prompt. Each state is mapped
to a dedicated handler that consumes persisted projections/evidence and returns a
bounded ``PhaseOutcome``. The server persists evidence and projection updates
before it performs at most one state transition.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Callable

from cptr.models import FactoryRun
from cptr.services.factory_domain import FactoryActor, FactoryState, is_terminal_factory_state
from cptr.services.factory_gates import EvidenceAuthority
from cptr.services.factory_phases import (
    PhaseArtifact,
    PhaseContext,
    PhaseHandler,
    PhaseOutcome,
)
from cptr.services.factory_store import SqlFactoryStore


class FactoryOrchestratorError(RuntimeError):
    pass


_WAITING_STATES = {FactoryState.PAUSED, FactoryState.APPROVAL_REQUIRED}


def _now_ms() -> int:
    return int(time.time() * 1000)


class FactoryOrchestrator:
    """Execute at most one durable phase action while owning the run lease."""

    def __init__(
        self,
        *,
        store: SqlFactoryStore,
        handlers: Mapping[FactoryState, PhaseHandler],
        owner_token: str,
        lease_ms: int,
        clock_ms: Callable[[], int] = _now_ms,
    ) -> None:
        owner_token = owner_token.strip()
        if not owner_token:
            raise ValueError("factory orchestrator owner token must not be blank")
        if lease_ms <= 0:
            raise ValueError("factory orchestrator lease duration must be positive")
        self._store = store
        self._handlers = dict(handlers)
        self._owner_token = owner_token
        self._lease_ms = int(lease_ms)
        self._clock_ms = clock_ms

    async def run_once(self, run_id: str) -> FactoryRun:
        observed = await self._required_run(run_id)
        observed_state = FactoryState(observed.state)
        if observed_state in _WAITING_STATES or is_terminal_factory_state(observed_state):
            return observed

        claimed = await self._store.claim_run(
            run_id,
            lease_token=self._owner_token,
            now_ms=self._clock_ms(),
            lease_ms=self._lease_ms,
        )
        if not claimed:
            return await self._required_run(run_id)

        try:
            run = await self._required_run(run_id)
            state = FactoryState(run.state)
            if state in _WAITING_STATES or is_terminal_factory_state(state):
                return run

            cycle = await self._ensure_cycle(run)
            run = await self._required_run(run_id)
            state_entry = await self._store.latest_state_entry(
                run.id,
                cycle_id=cycle.id,
                state=state,
            )
            if state_entry is None:
                raise FactoryOrchestratorError(
                    f"active state {state.value} has no durable state-entry event"
                )
            phase_key = self._phase_key(run, cycle, state, state_entry.id)
            handler = self._handlers.get(state)
            if handler is None:
                raise FactoryOrchestratorError(
                    f"no phase handler configured for active factory state {state.value}"
                )

            all_evidence = await self._store.list_evidence(run.id, limit=500)
            cycle_evidence = tuple(
                item for item in all_evidence if item.cycle_id in {None, cycle.id}
            )
            gates = tuple(await self._store.list_gates(run.id, cycle_id=cycle.id))
            outcome = await handler.execute(
                PhaseContext(
                    run=run,
                    cycle=cycle,
                    evidence=cycle_evidence,
                    gates=gates,
                )
            )
            if not isinstance(outcome, PhaseOutcome):
                raise FactoryOrchestratorError(
                    f"phase handler for {state.value} returned an invalid outcome"
                )

            evidence_ids, evidence_by_key = await self._persist_artifacts(
                run,
                cycle.id,
                outcome,
                phase_key=phase_key,
            )
            await self._persist_gates(
                run,
                cycle.id,
                outcome,
                evidence_by_key=evidence_by_key,
                existing_gates=gates,
                phase_key=phase_key,
            )

            if outcome.cycle_updates or outcome.run_next_action is not None:
                cycle = await self._store.update_cycle_projection(
                    run.id,
                    cycle.id,
                    updates=outcome.cycle_updates,
                    run_next_action=outcome.run_next_action,
                    idempotency_key=f"{phase_key}:projection",
                )
            if outcome.target_revision is not None and outcome.target_fingerprint is not None:
                cycle = await self._store.set_cycle_target(
                    run.id,
                    cycle.id,
                    revision=outcome.target_revision,
                    fingerprint=outcome.target_fingerprint,
                    idempotency_key=f"{phase_key}:target",
                )

            next_state = outcome.next_state
            if outcome.failure is not None:
                failure_artifact = PhaseArtifact(
                    key="__factory_failure__",
                    gate_id=outcome.failure.gate_id,
                    kind="phase_failure",
                    source="factory-orchestrator",
                    authority=EvidenceAuthority.MACHINE,
                    revision=cycle.target_revision,
                    fingerprint=cycle.target_fingerprint,
                    payload={
                        "category": outcome.failure.category.value,
                        "code": outcome.failure.code,
                        "gate_id": outcome.failure.gate_id,
                        "summary": outcome.failure.summary,
                        "signature": outcome.failure.signature,
                    },
                )
                failure_row = await self._store.append_evidence(
                    run_id=run.id,
                    cycle_id=cycle.id,
                    gate_id=failure_artifact.gate_id,
                    kind=failure_artifact.kind,
                    source=failure_artifact.source,
                    authority=failure_artifact.authority,
                    revision=failure_artifact.revision,
                    fingerprint=failure_artifact.fingerprint,
                    payload=failure_artifact.payload,
                    idempotency_key=f"{phase_key}:artifact:__factory_failure__",
                )
                evidence_ids.append(failure_row.id)
                cycle = await self._store.record_failure(
                    run.id,
                    cycle.id,
                    signature=outcome.failure.signature,
                    category=outcome.failure.category.value,
                    code=outcome.failure.code,
                    gate_id=outcome.failure.gate_id,
                    summary=outcome.failure.summary,
                    idempotency_key=f"{phase_key}:failure",
                )
                next_state = next_state or FactoryState.REPAIR_REQUIRED

            if state is FactoryState.CYCLE_COMPLETE and next_state is FactoryState.AUDITING:
                await self._store.advance_cycle(
                    run.id,
                    cycle.id,
                    reason=outcome.reason,
                    evidence_ids=tuple(evidence_ids),
                    idempotency_key=f"{phase_key}:transition",
                )
                return await self._required_run(run.id)

            if outcome.victory_decision is not None:
                if next_state is not FactoryState.COMMITTING:
                    raise FactoryOrchestratorError(
                        "Victory outcome must request the COMMITTING transition"
                    )
                await self._store.authorize_victory(
                    run.id,
                    cycle.id,
                    outcome.victory_decision,
                    idempotency_key=f"{phase_key}:victory",
                )
            elif next_state is not None:
                await self._store.transition(
                    run.id,
                    to_state=next_state,
                    actor=FactoryActor.SYSTEM,
                    reason=outcome.reason,
                    idempotency_key=f"{phase_key}:transition",
                    evidence_ids=tuple(evidence_ids),
                )
            return await self._required_run(run.id)
        finally:
            await self._store.release_run(run_id, lease_token=self._owner_token)

    async def _required_run(self, run_id: str) -> FactoryRun:
        run = await self._store.get_run(run_id)
        if run is None:
            raise KeyError("factory run not found")
        return run

    async def _ensure_cycle(self, run: FactoryRun):
        if run.current_cycle_id:
            cycle = await self._store.get_cycle(run.current_cycle_id)
            if cycle is None or cycle.run_id != run.id:
                raise FactoryOrchestratorError("active factory cycle projection is missing")
            return cycle
        if FactoryState(run.state) is not FactoryState.MISSION:
            raise FactoryOrchestratorError(
                f"active state {run.state} requires a persisted current cycle"
            )
        return await self._store.create_cycle(
            run.id,
            base_revision=None,
            base_fingerprint=None,
            idempotency_key=f"orchestrator-initial-cycle:{run.id}",
        )

    @staticmethod
    def _phase_key(run, cycle, state: FactoryState, state_entry_id: str) -> str:
        return f"phase:{run.id}:{cycle.id}:{state.value}:entry-{state_entry_id}"

    async def _persist_artifacts(
        self,
        run,
        cycle_id: str,
        outcome: PhaseOutcome,
        *,
        phase_key: str,
    ):
        evidence_ids: list[str] = []
        by_key: dict[str, str] = {}
        for artifact in outcome.artifacts:
            row = await self._store.append_evidence(
                run_id=run.id,
                cycle_id=cycle_id,
                gate_id=artifact.gate_id,
                kind=artifact.kind,
                source=artifact.source,
                authority=artifact.authority,
                revision=artifact.revision,
                fingerprint=artifact.fingerprint,
                payload=artifact.payload,
                idempotency_key=f"{phase_key}:artifact:{artifact.key}",
            )
            evidence_ids.append(row.id)
            by_key[artifact.key] = row.id
        return evidence_ids, by_key

    async def _persist_gates(
        self,
        run,
        cycle_id: str,
        outcome: PhaseOutcome,
        *,
        evidence_by_key: dict[str, str],
        existing_gates,
        phase_key: str,
    ) -> None:
        max_attempt: dict[str, int] = {}
        for row in existing_gates:
            max_attempt[row.gate_id] = max(max_attempt.get(row.gate_id, 0), int(row.attempt))
        for update in outcome.gates:
            missing = [key for key in update.artifact_keys if key not in evidence_by_key]
            if missing:
                raise FactoryOrchestratorError(
                    f"gate {update.gate_id} references unknown phase artifact {missing[0]}"
                )
            evidence_ids = tuple(update.evidence_ids) + tuple(
                evidence_by_key[key] for key in update.artifact_keys
            )
            gate_key = f"{phase_key}:gate:{update.gate_id}"
            replay = next(
                (
                    row
                    for row in existing_gates
                    if row.gate_id == update.gate_id and row.idempotency_key == gate_key
                ),
                None,
            )
            attempt = (
                int(replay.attempt)
                if replay is not None
                else max(int(update.attempt), max_attempt.get(update.gate_id, 0) + 1)
            )
            await self._store.record_gate(
                run_id=run.id,
                cycle_id=cycle_id,
                gate_id=update.gate_id,
                category=update.category.value,
                required=update.required,
                applicable=update.applicable,
                status=update.status.value,
                evidence_ids=evidence_ids,
                evaluated_revision=update.evaluated_revision,
                evaluated_fingerprint=update.evaluated_fingerprint,
                reason=update.reason,
                attempt=attempt,
                idempotency_key=gate_key,
            )
            max_attempt[update.gate_id] = attempt
