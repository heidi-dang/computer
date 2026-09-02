"""Restart-safe recovery coordination for durable Dark Factory runs."""

from __future__ import annotations

import time

from cptr.models import FactoryRun
from cptr.services.factory_domain import FactoryActor, FactoryState, is_terminal_factory_state
from cptr.services.factory_store import SqlFactoryStore


_WAITING_STATES = {
    FactoryState.RECOVERING,
    FactoryState.PAUSED,
    FactoryState.APPROVAL_REQUIRED,
}


def _now_ms() -> int:
    return int(time.time() * 1000)


class FactoryRuntime:
    """Recover durable runs without guessing that interrupted work succeeded."""

    def __init__(
        self,
        *,
        store: SqlFactoryStore,
        owner_token: str,
        lease_ms: int,
    ) -> None:
        owner_token = owner_token.strip()
        if not owner_token:
            raise ValueError("factory runtime owner token must not be blank")
        if lease_ms <= 0:
            raise ValueError("factory runtime lease duration must be positive")
        self._store = store
        self._owner_token = owner_token
        self._lease_ms = lease_ms

    async def recover_active_runs(self) -> list[str]:
        """Claim and reconcile each interrupted active run at most once per scan."""

        recovered: list[str] = []
        for observed in await self._store.list_recoverable():
            observed_state = FactoryState(observed.state)
            if observed_state in _WAITING_STATES:
                continue
            if is_terminal_factory_state(observed_state):
                continue

            claimed = await self._store.claim_run(
                observed.id,
                lease_token=self._owner_token,
                now_ms=_now_ms(),
                lease_ms=self._lease_ms,
            )
            if not claimed:
                continue

            try:
                current = await self._store.get_run(observed.id)
                if current is None or current.state != observed.state:
                    continue
                current_state = FactoryState(current.state)
                if current_state in _WAITING_STATES or is_terminal_factory_state(current_state):
                    continue

                await self.reconcile_run(
                    observed.id,
                    idempotency_key=(
                        f"restart-recovery:{observed.id}:{observed.state}:{observed.updated_at}"
                    ),
                )
                recovered.append(observed.id)
            finally:
                await self._store.release_run(observed.id, lease_token=self._owner_token)

        return recovered

    async def reconcile_run(
        self,
        run_id: str,
        *,
        idempotency_key: str | None = None,
    ) -> FactoryRun:
        """Move an interrupted run into RECOVERING while preserving its prior state."""

        run = await self._store.get_run(run_id)
        if run is None:
            raise KeyError("factory run not found")

        state = FactoryState(run.state)
        if state in _WAITING_STATES or is_terminal_factory_state(state):
            return run

        resumable_state = None if state is FactoryState.MISSION else state
        return await self._store.transition(
            run_id,
            to_state=FactoryState.RECOVERING,
            actor=FactoryActor.SYSTEM,
            reason="server restart requires execution reconciliation before continuation",
            idempotency_key=(
                idempotency_key
                or f"restart-recovery:{run.id}:{run.state}:{run.updated_at}"
            ),
            resumable_state=resumable_state,
        )
