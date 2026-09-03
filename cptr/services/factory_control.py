"""Owner-scoped, bounded control surface for durable Dark Factory runs.

This service is the server-authoritative boundary used by the authenticated
Control API. Routers do not encode the factory transition graph. User control
operations call the existing durable store and worker controller, while push
approvals are persisted as exact revision/remote/branch envelopes.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from cptr.models import FactoryApproval, FactoryCycle, FactoryRun
from cptr.services.factory_domain import FactoryActor, FactoryState, is_terminal_factory_state
from cptr.services.factory_git import PushAuthorization
from cptr.services.factory_store import SqlFactoryStore
from cptr.services.factory_workers import FactoryWorkerController
from cptr.utils.db import get_session_factory
from cptr.utils.redaction import redact_external, redact_text


class FactoryControlError(RuntimeError):
    code = "FACTORY_CONTROL_ERROR"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code:
            self.code = code


class FactoryControlNotFound(FactoryControlError):
    code = "FACTORY_NOT_FOUND"


class FactoryControlConflict(FactoryControlError):
    code = "FACTORY_CONTROL_CONFLICT"


_MAX_PAGE_LIMIT = 100
_DEFAULT_PAGE_BYTES = 256 * 1024
_MAX_MESSAGE_CHARS = 50_000


def _now_ms() -> int:
    return int(time.time() * 1000)


def _approval_id() -> str:
    return f"fapproval_{uuid.uuid4().hex}"


def _bounded_token(value: str, label: str, max_length: int = 500) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{label} must not be blank")
    if len(normalized) > max_length:
        raise ValueError(f"{label} exceeds bounded length")
    return normalized


def _digest(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _approval_dict(row: FactoryApproval) -> dict[str, Any]:
    return {
        "approval_id": row.id,
        "kind": row.kind,
        "status": row.status,
        "revision": row.revision,
        "remote": row.remote,
        "branch": row.branch,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "decided_at": row.decided_at,
    }


def _run_dict(run: FactoryRun) -> dict[str, Any]:
    return {
        "run_id": run.id,
        "workspace_id": run.workspace_id,
        "state": run.state,
        "current_cycle_id": run.current_cycle_id,
        "resumable_state": run.resumable_state,
        "next_action": run.next_action,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
        "completed_at": run.completed_at,
    }


def _event_dict(row) -> dict[str, Any]:
    return redact_external(
        {
            "event_id": row.id,
            "cycle_id": row.cycle_id,
            "sequence": int(row.sequence),
            "actor": row.actor,
            "event_type": row.event_type,
            "from_state": row.from_state,
            "to_state": row.to_state,
            "payload": row.payload,
            "created_at": row.created_at,
        }
    )


def _evidence_dict(row) -> dict[str, Any]:
    return redact_external(
        {
            "evidence_id": row.id,
            "cycle_id": row.cycle_id,
            "gate_id": row.gate_id,
            "kind": row.kind,
            "source": row.source,
            "authority": row.authority,
            "revision": row.revision,
            "fingerprint": row.fingerprint,
            "digest": row.digest,
            "payload": row.payload,
            "created_at": row.created_at,
        }
    )


def _bounded_page(
    rows: Sequence[Any],
    *,
    serializer,
    cursor_getter,
    limit: int,
    max_bytes: int,
) -> tuple[list[dict[str, Any]], str | None, int, bool]:
    output: list[dict[str, Any]] = []
    used = 0
    truncated_by_bytes = False
    candidates = list(rows[:limit])
    has_more = len(rows) > limit
    for row in candidates:
        item = serializer(row)
        encoded = json.dumps(item, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        if used + len(encoded) > max_bytes:
            truncated_by_bytes = True
            break
        output.append(item)
        used += len(encoded)
    if truncated_by_bytes:
        has_more = True
    next_cursor = str(cursor_getter(candidates[len(output) - 1])) if has_more and output else None
    return output, next_cursor, used, truncated_by_bytes


class FactoryControlService:
    def __init__(
        self,
        *,
        store: SqlFactoryStore | None = None,
        session_factory: async_sessionmaker | None = None,
        worker_controller: FactoryWorkerController | Any | None = None,
        max_page_bytes: int = _DEFAULT_PAGE_BYTES,
    ) -> None:
        if max_page_bytes <= 0:
            raise ValueError("factory control max_page_bytes must be positive")
        self._session_factory = session_factory or get_session_factory()
        self._store = store or SqlFactoryStore(session_factory=self._session_factory)
        self._workers = worker_controller or FactoryWorkerController()
        self._max_page_bytes = int(max_page_bytes)

    async def _owned_run(self, user_id: str, run_id: str) -> FactoryRun:
        run = await self._store.get_run(run_id, user_id=user_id)
        if run is None:
            raise FactoryControlNotFound("factory run not found")
        return run

    async def start(
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
        return await self._store.create_run(
            user_id=user_id,
            workspace_id=workspace_id,
            mission=mission,
            acceptance_criteria=acceptance_criteria,
            policy=policy,
            budget=budget,
            model_id=model_id,
            idempotency_key=idempotency_key,
        )

    async def status(self, *, user_id: str, run_id: str) -> dict[str, Any]:
        run = await self._owned_run(user_id, run_id)
        cycle = await self._store.get_cycle(run.current_cycle_id) if run.current_cycle_id else None
        gates = await self._store.list_gates(run.id, cycle_id=cycle.id) if cycle is not None else []
        latest: dict[str, Any] = {}
        for gate in gates:
            current = latest.get(gate.gate_id)
            if current is None or int(gate.attempt) >= int(current.attempt):
                latest[gate.gate_id] = gate
        gate_summary = [
            {
                "gate_id": gate.gate_id,
                "category": gate.category,
                "required": bool(gate.required),
                "applicable": bool(gate.applicable),
                "status": gate.status,
                "attempt": int(gate.attempt),
                "reason": gate.reason,
            }
            for gate in sorted(latest.values(), key=lambda item: item.gate_id)
        ]
        required = [item for item in gate_summary if item["required"] and item["applicable"]]
        passed = [item for item in required if item["status"] == "PASS"]
        pending_approval = await self._pending_approval(run.id)
        payload = {
            **_run_dict(run),
            "mission": run.mission,
            "acceptance_criteria": list(run.acceptance_criteria or []),
            "policy": run.policy,
            "budget": run.budget,
            "cycle": (
                {
                    "cycle_id": cycle.id,
                    "ordinal": int(cycle.ordinal),
                    "state": cycle.state,
                    "selected_finding": cycle.selected_finding,
                    "selected_capabilities": list(cycle.selected_capabilities or []),
                    "attempt_count": int(cycle.attempt_count or 0),
                    "next_action": cycle.next_action,
                    "target_revision": cycle.target_revision,
                    "target_fingerprint": cycle.target_fingerprint,
                }
                if cycle is not None
                else None
            ),
            "progress": {
                "cycle_ordinal": int(cycle.ordinal) if cycle is not None else 0,
                "required_gates": len(required),
                "passed_required_gates": len(passed),
            },
            "pending_approval": _approval_dict(pending_approval) if pending_approval else None,
            "gates": gate_summary,
        }
        return redact_external(payload)

    async def events(
        self,
        *,
        user_id: str,
        run_id: str,
        cursor: str | None,
        limit: int,
    ) -> dict[str, Any]:
        await self._owned_run(user_id, run_id)
        limit = max(1, min(int(limit), _MAX_PAGE_LIMIT))
        try:
            after_sequence = int(cursor) if cursor else 0
        except (TypeError, ValueError) as exc:
            raise FactoryControlConflict("invalid factory event cursor", code="FACTORY_INVALID_CURSOR") from exc
        if after_sequence < 0:
            raise FactoryControlConflict("invalid factory event cursor", code="FACTORY_INVALID_CURSOR")
        rows = await self._store.list_events(run_id, after_sequence=after_sequence, limit=limit + 1)
        items, next_cursor, used, byte_truncated = _bounded_page(
            rows,
            serializer=_event_dict,
            cursor_getter=lambda row: row.sequence,
            limit=limit,
            max_bytes=self._max_page_bytes,
        )
        return {
            "events": items,
            "next_cursor": next_cursor,
            "max_bytes": self._max_page_bytes,
            "bytes_returned": used,
            "truncated": byte_truncated,
        }

    async def evidence(
        self,
        *,
        user_id: str,
        run_id: str,
        cursor: str | None,
        limit: int,
    ) -> dict[str, Any]:
        await self._owned_run(user_id, run_id)
        limit = max(1, min(int(limit), _MAX_PAGE_LIMIT))
        try:
            rows = await self._store.list_evidence_page(run_id, after_id=cursor, limit=limit + 1)
        except ValueError as exc:
            raise FactoryControlConflict("invalid factory evidence cursor", code="FACTORY_INVALID_CURSOR") from exc
        items, next_cursor, used, byte_truncated = _bounded_page(
            rows,
            serializer=_evidence_dict,
            cursor_getter=lambda row: row.id,
            limit=limit,
            max_bytes=self._max_page_bytes,
        )
        return {
            "evidence": items,
            "next_cursor": next_cursor,
            "max_bytes": self._max_page_bytes,
            "bytes_returned": used,
            "truncated": byte_truncated,
        }

    async def message(
        self,
        *,
        user_id: str,
        run_id: str,
        content: str,
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        run = await self._owned_run(user_id, run_id)
        content = str(content or "").strip()
        if not content:
            raise ValueError("factory message must not be blank")
        if len(content) > _MAX_MESSAGE_CHARS:
            raise ValueError("factory message exceeds bounded length")
        event = await self._store.append_user_event(
            run_id=run.id,
            event_type="user.message",
            payload={"content": content},
            idempotency_key=f"message:{idempotency_key}" if idempotency_key else None,
        )
        return {"event": _event_dict(event)}

    async def pause(
        self,
        *,
        user_id: str,
        run_id: str,
        idempotency_key: str,
    ) -> FactoryRun:
        run = await self._owned_run(user_id, run_id)
        state = FactoryState(run.state)
        if state is FactoryState.PAUSED:
            return run
        if state is FactoryState.APPROVAL_REQUIRED:
            raise FactoryControlConflict(
                "approval-required runs cannot be paused or resumed through the generic pause surface",
                code="FACTORY_APPROVAL_REQUIRED",
            )
        if is_terminal_factory_state(state):
            raise FactoryControlConflict("terminal factory run cannot be paused", code="FACTORY_TERMINAL")
        return await self._store.transition(
            run.id,
            to_state=FactoryState.PAUSED,
            actor=FactoryActor.USER,
            reason="authenticated user paused the factory run",
            idempotency_key=f"user-pause:{idempotency_key}",
        )

    async def resume(
        self,
        *,
        user_id: str,
        run_id: str,
        idempotency_key: str,
    ) -> FactoryRun:
        run = await self._owned_run(user_id, run_id)
        state = FactoryState(run.state)
        if state is FactoryState.APPROVAL_REQUIRED:
            raise FactoryControlConflict(
                "factory run requires the exact pending approval; generic resume cannot bypass it",
                code="FACTORY_APPROVAL_REQUIRED",
            )
        if state is not FactoryState.PAUSED:
            raise FactoryControlConflict("factory run is not paused", code="FACTORY_NOT_PAUSED")
        if not run.resumable_state:
            raise FactoryControlConflict("paused factory run has no resumable state", code="FACTORY_RESUME_STATE_MISSING")
        return await self._store.transition(
            run.id,
            to_state=FactoryState(run.resumable_state),
            actor=FactoryActor.USER,
            reason="authenticated user resumed the paused factory run",
            idempotency_key=f"user-resume:{idempotency_key}",
        )

    async def request_approval(
        self,
        *,
        run_id: str,
        cycle_id: str,
        kind: str,
        revision: str,
        remote: str,
        branch: str,
    ) -> FactoryApproval:
        kind = _bounded_token(kind, "approval kind", 120).lower()
        revision = _bounded_token(revision, "approval revision")
        remote = _bounded_token(remote, "approval remote", 200)
        branch = _bounded_token(branch, "approval branch", 300)
        envelope = {"kind": kind, "revision": revision, "remote": remote, "branch": branch}
        operation_digest = _digest(envelope)
        now = _now_ms()
        async with self._session_factory() as db:
            async with db.begin():
                run = await db.get(FactoryRun, run_id)
                cycle = await db.get(FactoryCycle, cycle_id)
                if run is None or cycle is None or cycle.run_id != run_id:
                    raise FactoryControlNotFound("factory run not found")
                pending = (
                    await db.execute(
                        select(FactoryApproval).where(
                            FactoryApproval.cycle_id == cycle_id,
                            FactoryApproval.kind == kind,
                            FactoryApproval.status == "PENDING",
                        )
                    )
                ).scalars().first()
                if pending is not None and pending.operation_digest != operation_digest:
                    raise FactoryControlConflict(
                        "a different approval envelope is already pending for this cycle",
                        code="FACTORY_APPROVAL_CONFLICT",
                    )
                if pending is not None:
                    return pending
                row_id = _approval_id()
                await db.execute(
                    sqlite_insert(FactoryApproval)
                    .values(
                        id=row_id,
                        run_id=run_id,
                        cycle_id=cycle_id,
                        kind=kind,
                        operation_digest=operation_digest,
                        revision=revision,
                        remote=remote,
                        branch=branch,
                        status="PENDING",
                        created_at=now,
                        updated_at=now,
                    )
                    .on_conflict_do_nothing(index_elements=["cycle_id", "kind", "operation_digest"])
                )
                row = (
                    await db.execute(
                        select(FactoryApproval).where(
                            FactoryApproval.cycle_id == cycle_id,
                            FactoryApproval.kind == kind,
                            FactoryApproval.operation_digest == operation_digest,
                        )
                    )
                ).scalar_one()
                return row

    async def approve(
        self,
        *,
        user_id: str,
        run_id: str,
        approval_id: str,
        approved: bool,
        note: str | None,
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        run = await self._owned_run(user_id, run_id)
        approval_id = _bounded_token(approval_id, "approval ID", 200)
        decision_key = _bounded_token(
            idempotency_key or f"decision-{approval_id}", "approval idempotency key", 200
        )
        safe_note = redact_text(note.strip())[:4_000] if note and note.strip() else None
        decision_digest = _digest({"approved": bool(approved), "note": safe_note})
        async with self._session_factory() as db:
            async with db.begin():
                approval = await db.get(FactoryApproval, approval_id)
                persistent_run = await db.get(FactoryRun, run.id)
                if approval is None or approval.run_id != run.id or persistent_run is None:
                    raise FactoryControlNotFound("factory run not found")
                if (
                    approval.status == "PENDING"
                    and persistent_run.state != FactoryState.APPROVAL_REQUIRED.value
                ):
                    raise FactoryControlConflict(
                        "factory run is not waiting for this approval",
                        code="FACTORY_NOT_APPROVAL_REQUIRED",
                    )
                if approval.status != "PENDING":
                    if (
                        approval.decision_digest == decision_digest
                        and approval.decision_idempotency_key == decision_key
                    ):
                        decided = approval
                    else:
                        raise FactoryControlConflict(
                            "factory approval was already decided under a different decision envelope",
                            code="FACTORY_APPROVAL_REPLAY_CONFLICT",
                        )
                else:
                    existing_key = (
                        await db.execute(
                            select(FactoryApproval).where(
                                FactoryApproval.run_id == run.id,
                                FactoryApproval.decision_idempotency_key == decision_key,
                            )
                        )
                    ).scalar_one_or_none()
                    if existing_key is not None and existing_key.id != approval.id:
                        raise FactoryControlConflict(
                            "factory approval idempotency key is already bound to another approval",
                            code="FACTORY_APPROVAL_REPLAY_CONFLICT",
                        )
                    approval.status = "APPROVED" if approved else "DENIED"
                    approval.decision_idempotency_key = decision_key
                    approval.decision_digest = decision_digest
                    approval.note = safe_note
                    approval.decided_at = _now_ms()
                    approval.updated_at = approval.decided_at
                    decided = approval

        await self._store.append_user_event(
            run_id=run.id,
            cycle_id=decided.cycle_id,
            event_type="approval.decision",
            payload={
                "approval_id": decided.id,
                "kind": decided.kind,
                "status": decided.status,
                "revision": decided.revision,
                "remote": decided.remote,
                "branch": decided.branch,
            },
            idempotency_key=f"approval-event:{decided.id}:{decision_key}",
        )
        current = await self._owned_run(user_id, run.id)
        if current.state == FactoryState.APPROVAL_REQUIRED.value:
            if approved:
                if not current.resumable_state:
                    raise FactoryControlConflict(
                        "approval-required factory run has no resumable state",
                        code="FACTORY_RESUME_STATE_MISSING",
                    )
                current = await self._store.transition(
                    current.id,
                    to_state=FactoryState(current.resumable_state),
                    actor=FactoryActor.USER,
                    reason=f"authenticated user approved exact {decided.kind} envelope",
                    idempotency_key=f"approval-release:{decided.id}",
                )
            else:
                current = await self._store.transition(
                    current.id,
                    to_state=FactoryState.BLOCKED,
                    actor=FactoryActor.SYSTEM,
                    reason=f"authenticated user denied required {decided.kind} approval",
                    idempotency_key=f"approval-denied:{decided.id}",
                )
        return {"approval": _approval_dict(decided), "run": _run_dict(current)}

    async def stop(
        self,
        *,
        user_id: str,
        run_id: str,
        idempotency_key: str,
        timeout_ms: int,
    ) -> FactoryRun:
        run = await self._owned_run(user_id, run_id)
        state = FactoryState(run.state)
        if state is FactoryState.CANCELLED:
            return run
        if is_terminal_factory_state(state):
            raise FactoryControlConflict("terminal factory run cannot be stopped", code="FACTORY_TERMINAL")
        result = await self._workers.cancel_run(run, timeout_ms=timeout_ms)
        if not result.quiescent:
            raise FactoryControlConflict(
                "owned factory execution did not quiesce within the cancellation bound",
                code="FACTORY_CANCELLATION_NOT_QUIESCENT",
            )
        return await self._store.transition(
            run.id,
            to_state=FactoryState.CANCELLED,
            actor=FactoryActor.USER,
            reason="authenticated user stopped the factory run after owned execution quiesced",
            idempotency_key=f"user-stop:{idempotency_key}",
        )

    async def resolve_push_authorization(
        self,
        *,
        run_id: str,
        cycle_id: str,
        revision: str,
        remote: str,
        branch: str,
    ) -> PushAuthorization | None:
        approval = await self.request_approval(
            run_id=run_id,
            cycle_id=cycle_id,
            kind="git_push",
            revision=revision,
            remote=remote,
            branch=branch,
        )
        if approval.status == "PENDING":
            return None
        if approval.status != "APPROVED":
            raise FactoryControlConflict(
                "required factory push approval was denied",
                code="FACTORY_APPROVAL_DENIED",
            )
        return PushAuthorization(
            approved=True,
            approval_id=approval.id,
            revision=approval.revision,
            remote=approval.remote,
            branch=approval.branch,
        )

    async def _pending_approval(self, run_id: str) -> FactoryApproval | None:
        async with self._session_factory() as db:
            return (
                await db.execute(
                    select(FactoryApproval)
                    .where(
                        FactoryApproval.run_id == run_id,
                        FactoryApproval.status == "PENDING",
                    )
                    .order_by(FactoryApproval.updated_at.desc(), FactoryApproval.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
