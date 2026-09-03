"""Read-only, owner-scoped observability snapshots for the Dark Factory dashboard."""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from cptr.models import (
    FactoryApproval,
    FactoryCapabilityOutcome,
    FactoryCapabilityRecord,
    FactoryCiRun,
    FactoryCommitIntent,
    FactoryCycle,
    FactoryEvent,
    FactoryEvidence,
    FactoryGateResult,
    FactoryMetricProjection,
    FactoryReasoningCall,
    FactoryRun,
    FactoryWorkerAssignment,
    Workspace,
)
from cptr.services.factory_domain import FactoryState, is_terminal_factory_state
from cptr.utils.db import get_session_factory
from cptr.utils.redaction import redact_external

_MAX_RUNS = 30
_MAX_CYCLES = 32
_MAX_EVENTS = 160
_MAX_EVIDENCE = 120
_MAX_REASONING = 80
_MAX_WORKERS = 64
_MAX_APPROVALS = 32
_MAX_METRICS = 120
_MAX_CAPABILITY_OUTCOMES = 80
_MAX_CI_RUNS = 80
_MAX_PAYLOAD_BYTES = 8 * 1024

# Canonical success-path phases. Transient states (pause, approval, repair,
# failure) project onto the last/effective phase rather than inventing client
# percentages. This keeps progress server-authoritative and auditable.
_PROGRESS_STATES = (
    FactoryState.MISSION,
    FactoryState.RECOVERING,
    FactoryState.BASELINING,
    FactoryState.UNDERSTANDING,
    FactoryState.AUDITING,
    FactoryState.SELECTING_FINDING,
    FactoryState.CAPABILITY_ANALYSIS,
    FactoryState.SKILL_DISCOVERY,
    FactoryState.TRUST_EVALUATION,
    FactoryState.SKILL_SELECTION,
    FactoryState.REPRODUCING,
    FactoryState.ROOT_CAUSE_ANALYSIS,
    FactoryState.PLANNING,
    FactoryState.IMPLEMENTING,
    FactoryState.TARGETED_VERIFYING,
    FactoryState.FULL_VERIFYING,
    FactoryState.ADVERSARIAL_REVIEW,
    FactoryState.SECURITY_REVIEW,
    FactoryState.LIVE_VERIFYING,
    FactoryState.VICTORY_JUDGING,
    FactoryState.COMMITTING,
    FactoryState.PUSHING,
    FactoryState.CI_VERIFYING,
    FactoryState.CYCLE_COMPLETE,
    FactoryState.COMPLETE,
)
_PROGRESS_INDEX = {state.value: index for index, state in enumerate(_PROGRESS_STATES)}


def _now_ms() -> int:
    return int(time.time() * 1000)


def _bounded_payload(value: Any, *, max_bytes: int = _MAX_PAYLOAD_BYTES) -> Any:
    safe = redact_external(value)
    encoded = json.dumps(
        safe,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    if len(encoded) <= max_bytes:
        return safe
    return {
        "truncated": True,
        "bytes": len(encoded),
        "preview": encoded[: max(0, max_bytes - 256)].decode("utf-8", errors="replace"),
    }


def _progress_dict(run: FactoryRun, events: list[FactoryEvent]) -> dict[str, Any]:
    state = FactoryState(run.state)
    effective_state = state.value

    if state in {
        FactoryState.RECOVERING,
        FactoryState.PAUSED,
        FactoryState.APPROVAL_REQUIRED,
        FactoryState.REPAIR_REQUIRED,
        FactoryState.BLOCKED,
        FactoryState.FAILED,
        FactoryState.CANCELLED,
    }:
        if run.resumable_state in _PROGRESS_INDEX:
            effective_state = str(run.resumable_state)
        elif state is not FactoryState.RECOVERING or run.resumable_state:
            for event in reversed(events):
                for candidate in (event.to_state, event.from_state):
                    if candidate in _PROGRESS_INDEX:
                        effective_state = str(candidate)
                        break
                if effective_state in _PROGRESS_INDEX:
                    break

    index = _PROGRESS_INDEX.get(effective_state, 0)
    denominator = max(1, len(_PROGRESS_STATES) - 1)
    percent = max(0, min(100, round((index / denominator) * 100)))
    if state is FactoryState.COMPLETE:
        percent = 100

    if state is FactoryState.COMPLETE:
        outcome = "success"
    elif state is FactoryState.FAILED:
        outcome = "failed"
    elif state is FactoryState.BLOCKED:
        outcome = "blocked"
    elif state is FactoryState.CANCELLED:
        outcome = "cancelled"
    elif state is FactoryState.PAUSED:
        outcome = "paused"
    elif state is FactoryState.APPROVAL_REQUIRED:
        outcome = "approval_required"
    elif state is FactoryState.REPAIR_REQUIRED:
        outcome = "repairing"
    elif state is FactoryState.RECOVERING and run.resumable_state:
        outcome = "recovering"
    else:
        outcome = "running"

    return {
        "percent": percent,
        "state": state.value,
        "effective_state": effective_state,
        "phase_index": index + 1,
        "phase_count": len(_PROGRESS_STATES),
        "outcome": outcome,
        "terminal": is_terminal_factory_state(state),
        "basis": "server_state_machine",
        "updated_at_ms": int(run.updated_at),
    }


def _run_summary(run: FactoryRun, workspace_name: str | None) -> dict[str, Any]:
    state = FactoryState(run.state)
    return {
        "run_id": run.id,
        "workspace_id": run.workspace_id,
        "workspace_name": workspace_name,
        "mission": run.mission[:500],
        "state": run.state,
        "terminal": is_terminal_factory_state(state),
        "current_cycle_id": run.current_cycle_id,
        "next_action": run.next_action,
        "created_at": int(run.created_at),
        "updated_at": int(run.updated_at),
        "completed_at": int(run.completed_at) if run.completed_at is not None else None,
    }


def _cycle_dict(row: FactoryCycle) -> dict[str, Any]:
    return {
        "cycle_id": row.id,
        "ordinal": int(row.ordinal),
        "state": row.state,
        "selected_finding": _bounded_payload(row.selected_finding),
        "capability_requirements": _bounded_payload(list(row.capability_requirements or [])),
        "selected_capabilities": _bounded_payload(list(row.selected_capabilities or [])),
        "gate_plan": _bounded_payload(row.gate_plan or {}),
        "base_revision": row.base_revision,
        "base_fingerprint": row.base_fingerprint,
        "target_revision": row.target_revision,
        "target_fingerprint": row.target_fingerprint,
        "mutation_worker_id": row.mutation_worker_id,
        "attempt_count": int(row.attempt_count or 0),
        "failure_signatures": _bounded_payload(row.failure_signatures or {}),
        "next_action": row.next_action,
        "created_at": int(row.created_at),
        "updated_at": int(row.updated_at),
        "completed_at": int(row.completed_at) if row.completed_at is not None else None,
    }


def _gate_dict(row: FactoryGateResult) -> dict[str, Any]:
    return {
        "gate_result_id": row.id,
        "cycle_id": row.cycle_id,
        "gate_id": row.gate_id,
        "category": row.category,
        "required": bool(row.required),
        "applicable": bool(row.applicable),
        "status": row.status,
        "evidence_ids": list(row.evidence_ids or []),
        "evaluated_revision": row.evaluated_revision,
        "evaluated_fingerprint": row.evaluated_fingerprint,
        "reason": row.reason,
        "attempt": int(row.attempt),
        "created_at": int(row.created_at),
        "updated_at": int(row.updated_at),
    }


def _event_dict(row: FactoryEvent) -> dict[str, Any]:
    return {
        "event_id": row.id,
        "cycle_id": row.cycle_id,
        "sequence": int(row.sequence),
        "actor": row.actor,
        "event_type": row.event_type,
        "from_state": row.from_state,
        "to_state": row.to_state,
        "payload": _bounded_payload(row.payload or {}),
        "created_at": int(row.created_at),
    }


def _evidence_dict(row: FactoryEvidence) -> dict[str, Any]:
    return {
        "evidence_id": row.id,
        "cycle_id": row.cycle_id,
        "gate_id": row.gate_id,
        "kind": row.kind,
        "source": row.source,
        "authority": row.authority,
        "revision": row.revision,
        "fingerprint": row.fingerprint,
        "digest": row.digest,
        "payload": _bounded_payload(row.payload or {}),
        "created_at": int(row.created_at),
    }


def _worker_dict(row: FactoryWorkerAssignment) -> dict[str, Any]:
    return {
        "assignment_id": row.id,
        "cycle_id": row.cycle_id,
        "worker_id": row.worker_id,
        "mode": row.mode,
        "repo_path": row.repo_path,
        "scope": list(row.scope or []),
        "branch": row.branch,
        "base_revision": row.base_revision,
        "status": row.status,
        "created_at": int(row.created_at),
        "updated_at": int(row.updated_at),
        "closed_at": int(row.closed_at) if row.closed_at is not None else None,
    }


def _reasoning_dict(row: FactoryReasoningCall) -> dict[str, Any]:
    return {
        "reasoning_id": row.id,
        "cycle_id": row.cycle_id,
        "role": row.role,
        "role_ordinal": int(row.role_ordinal),
        "schema_id": row.schema_id,
        "provider": row.provider,
        "model": row.model,
        "response_id": row.response_id,
        "input_tokens": int(row.input_tokens or 0),
        "output_tokens": int(row.output_tokens or 0),
        "total_tokens": int(row.total_tokens or 0),
        "runtime_ms": int(row.runtime_ms or 0),
        "cost_microusd": int(row.cost_microusd or 0),
        "attempt_count": int(row.attempt_count or 0),
        "data": _bounded_payload(row.data or {}),
        "provider_metadata": _bounded_payload(row.provider_metadata or {}, max_bytes=2 * 1024),
        "created_at": int(row.created_at),
    }


def _approval_dict(row: FactoryApproval) -> dict[str, Any]:
    return {
        "approval_id": row.id,
        "cycle_id": row.cycle_id,
        "kind": row.kind,
        "revision": row.revision,
        "remote": row.remote,
        "branch": row.branch,
        "status": row.status,
        "note": row.note,
        "created_at": int(row.created_at),
        "updated_at": int(row.updated_at),
        "decided_at": int(row.decided_at) if row.decided_at is not None else None,
    }


def _metric_dict(row: FactoryMetricProjection) -> dict[str, Any]:
    return {
        "metric_id": row.id,
        "cycle_id": row.cycle_id,
        "scope": row.scope,
        "dimension_key": row.dimension_key,
        "attempts": int(row.attempts or 0),
        "repair_iterations": int(row.repair_iterations or 0),
        "regressions": int(row.regressions or 0),
        "input_tokens": int(row.input_tokens or 0),
        "output_tokens": int(row.output_tokens or 0),
        "runtime_ms": int(row.runtime_ms or 0),
        "cost_microusd": int(row.cost_microusd or 0),
        "gate_latency_ms": int(row.gate_latency_ms or 0),
        "verified_outcome": row.verified_outcome,
        "updated_at": int(row.updated_at),
    }


def _capability_outcome_dict(
    outcome: FactoryCapabilityOutcome, capability: FactoryCapabilityRecord
) -> dict[str, Any]:
    return {
        "outcome_id": outcome.id,
        "cycle_id": outcome.cycle_id,
        "capability_id": outcome.capability_id,
        "stable_id": capability.stable_id,
        "version": capability.version,
        "origin_type": capability.origin_type,
        "risk_classification": capability.risk_classification,
        "trust_status": capability.trust_status,
        "verification_status": capability.verification_status,
        "repository_family": outcome.repository_family,
        "task_family": outcome.task_family,
        "verified_success": bool(outcome.verified_success),
        "regression": bool(outcome.regression),
        "repair_iterations": int(outcome.repair_iterations or 0),
        "input_tokens": int(outcome.input_tokens or 0),
        "output_tokens": int(outcome.output_tokens or 0),
        "runtime_ms": int(outcome.runtime_ms or 0),
        "cost_microusd": int(outcome.cost_microusd or 0),
        "created_at": int(outcome.created_at),
    }


def _commit_dict(row: FactoryCommitIntent) -> dict[str, Any]:
    return {
        "commit_intent_id": row.id,
        "cycle_id": row.cycle_id,
        "repository_key": row.repository_key,
        "verified_revision": row.verified_revision,
        "verified_fingerprint": row.verified_fingerprint,
        "diff_digest": row.diff_digest,
        "changed_paths": list(row.changed_paths or []),
        "commit_message": row.commit_message,
        "status": row.status,
        "commit_sha": row.commit_sha,
        "push_status": row.push_status,
        "push_remote": row.push_remote,
        "push_branch": row.push_branch,
        "push_approval_id": row.push_approval_id,
        "created_at": int(row.created_at),
        "updated_at": int(row.updated_at),
        "committed_at": int(row.committed_at) if row.committed_at is not None else None,
        "pushed_at": int(row.pushed_at) if row.pushed_at is not None else None,
    }


def _ci_dict(row: FactoryCiRun) -> dict[str, Any]:
    return {
        "ci_run_id": row.id,
        "cycle_id": row.cycle_id,
        "provider": row.provider,
        "repository": row.repository,
        "revision": row.revision,
        "external_run_id": row.external_run_id,
        "check_id": row.check_id,
        "status": row.status,
        "conclusion": row.conclusion,
        "url": row.url,
        "failure_summary": row.failure_summary,
        "diagnosis_required": bool(row.diagnosis_required),
        "diagnosis_summary": row.diagnosis_summary,
        "created_at": int(row.created_at),
        "updated_at": int(row.updated_at),
        "last_observed_at": int(row.last_observed_at) if row.last_observed_at is not None else None,
        "diagnosed_at": int(row.diagnosed_at) if row.diagnosed_at is not None else None,
    }


class FactoryObservabilityService:
    """Build bounded dashboard snapshots from persisted factory facts only."""

    def __init__(self, *, session_factory: async_sessionmaker | None = None) -> None:
        self._session_factory = session_factory or get_session_factory()

    async def activity_since(
        self,
        *,
        user_id: str,
        run_id: str,
        after_sequence: int,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Return every persisted activity event after a durable sequence cursor."""
        limit = max(1, min(int(limit), 500))
        cursor = max(0, int(after_sequence))
        async with self._session_factory() as db:
            owner = await db.scalar(
                select(FactoryRun.id).where(
                    FactoryRun.id == run_id,
                    FactoryRun.user_id == user_id,
                )
            )
            if owner is None:
                raise KeyError("factory run not found")
            rows = list(
                (
                    await db.scalars(
                        select(FactoryEvent)
                        .where(
                            FactoryEvent.run_id == run_id,
                            FactoryEvent.sequence > cursor,
                        )
                        .order_by(FactoryEvent.sequence.asc())
                        .limit(limit)
                    )
                ).all()
            )
        return [_event_dict(row) for row in rows]

    async def snapshot(
        self,
        *,
        user_id: str,
        run_id: str | None = None,
        run_limit: int = 20,
    ) -> dict[str, Any]:
        run_limit = max(1, min(int(run_limit), _MAX_RUNS))
        async with self._session_factory() as db:
            runs = list(
                (
                    await db.scalars(
                        select(FactoryRun)
                        .where(FactoryRun.user_id == user_id)
                        .order_by(FactoryRun.updated_at.desc(), FactoryRun.id.desc())
                        .limit(run_limit)
                    )
                ).all()
            )
            selected: FactoryRun | None = None
            if run_id:
                selected = (
                    await db.scalars(
                        select(FactoryRun).where(
                            FactoryRun.id == run_id,
                            FactoryRun.user_id == user_id,
                        )
                    )
                ).first()
                if selected is None:
                    raise KeyError("factory run not found")
                if all(row.id != selected.id for row in runs):
                    runs.insert(0, selected)
            elif runs:
                selected = runs[0]

            workspace_ids = {row.workspace_id for row in runs}
            workspace_names: dict[str, str] = {}
            if workspace_ids:
                workspaces = list(
                    (
                        await db.scalars(select(Workspace).where(Workspace.id.in_(workspace_ids)))
                    ).all()
                )
                workspace_names = {row.id: row.name for row in workspaces}

            run_summaries = [
                _run_summary(row, workspace_names.get(row.workspace_id)) for row in runs[:run_limit]
            ]
            if selected is None:
                core = {"version": 1, "runs": run_summaries, "selected": None}
                fingerprint = hashlib.sha256(
                    json.dumps(core, sort_keys=True, separators=(",", ":")).encode("utf-8")
                ).hexdigest()
                return {**core, "fingerprint": fingerprint, "generated_at_ms": _now_ms()}

            rid = selected.id
            cycles = list(
                (
                    await db.scalars(
                        select(FactoryCycle)
                        .where(FactoryCycle.run_id == rid)
                        .order_by(FactoryCycle.ordinal.asc())
                        .limit(_MAX_CYCLES)
                    )
                ).all()
            )
            current_cycle = next(
                (row for row in cycles if row.id == selected.current_cycle_id),
                cycles[-1] if cycles else None,
            )

            gate_rows = list(
                (
                    await db.scalars(
                        select(FactoryGateResult)
                        .where(FactoryGateResult.run_id == rid)
                        .order_by(FactoryGateResult.updated_at.desc(), FactoryGateResult.id.desc())
                        .limit(300)
                    )
                ).all()
            )
            current_gate_rows = [
                row
                for row in gate_rows
                if current_cycle is not None and row.cycle_id == current_cycle.id
            ]
            latest_gates: dict[str, FactoryGateResult] = {}
            for row in current_gate_rows:
                previous = latest_gates.get(row.gate_id)
                if previous is None or int(row.attempt) > int(previous.attempt):
                    latest_gates[row.gate_id] = row
            gate_list = sorted(
                (_gate_dict(row) for row in latest_gates.values()), key=lambda row: row["gate_id"]
            )

            event_rows_desc = list(
                (
                    await db.scalars(
                        select(FactoryEvent)
                        .where(FactoryEvent.run_id == rid)
                        .order_by(FactoryEvent.sequence.desc())
                        .limit(_MAX_EVENTS)
                    )
                ).all()
            )
            event_rows = list(reversed(event_rows_desc))
            evidence_rows = list(
                (
                    await db.scalars(
                        select(FactoryEvidence)
                        .where(FactoryEvidence.run_id == rid)
                        .order_by(FactoryEvidence.created_at.desc(), FactoryEvidence.id.desc())
                        .limit(_MAX_EVIDENCE)
                    )
                ).all()
            )
            worker_rows = list(
                (
                    await db.scalars(
                        select(FactoryWorkerAssignment)
                        .where(FactoryWorkerAssignment.run_id == rid)
                        .order_by(
                            FactoryWorkerAssignment.updated_at.desc(),
                            FactoryWorkerAssignment.id.desc(),
                        )
                        .limit(_MAX_WORKERS)
                    )
                ).all()
            )
            reasoning_rows = list(
                (
                    await db.scalars(
                        select(FactoryReasoningCall)
                        .where(FactoryReasoningCall.run_id == rid)
                        .order_by(
                            FactoryReasoningCall.created_at.desc(), FactoryReasoningCall.id.desc()
                        )
                        .limit(_MAX_REASONING)
                    )
                ).all()
            )
            approval_rows = list(
                (
                    await db.scalars(
                        select(FactoryApproval)
                        .where(FactoryApproval.run_id == rid)
                        .order_by(FactoryApproval.updated_at.desc(), FactoryApproval.id.desc())
                        .limit(_MAX_APPROVALS)
                    )
                ).all()
            )
            metric_rows = list(
                (
                    await db.scalars(
                        select(FactoryMetricProjection)
                        .where(FactoryMetricProjection.run_id == rid)
                        .order_by(
                            FactoryMetricProjection.updated_at.desc(),
                            FactoryMetricProjection.id.desc(),
                        )
                        .limit(_MAX_METRICS)
                    )
                ).all()
            )
            capability_rows = list(
                (
                    await db.execute(
                        select(FactoryCapabilityOutcome, FactoryCapabilityRecord)
                        .join(
                            FactoryCapabilityRecord,
                            FactoryCapabilityRecord.id == FactoryCapabilityOutcome.capability_id,
                        )
                        .where(FactoryCapabilityOutcome.run_id == rid)
                        .order_by(
                            FactoryCapabilityOutcome.created_at.desc(),
                            FactoryCapabilityOutcome.id.desc(),
                        )
                        .limit(_MAX_CAPABILITY_OUTCOMES)
                    )
                ).all()
            )
            commit_rows = list(
                (
                    await db.scalars(
                        select(FactoryCommitIntent)
                        .where(FactoryCommitIntent.run_id == rid)
                        .order_by(
                            FactoryCommitIntent.updated_at.desc(), FactoryCommitIntent.id.desc()
                        )
                        .limit(_MAX_CYCLES)
                    )
                ).all()
            )
            ci_rows = list(
                (
                    await db.scalars(
                        select(FactoryCiRun)
                        .where(FactoryCiRun.run_id == rid)
                        .order_by(FactoryCiRun.updated_at.desc(), FactoryCiRun.id.desc())
                        .limit(_MAX_CI_RUNS)
                    )
                ).all()
            )

            totals = (
                await db.execute(
                    select(
                        func.count(FactoryReasoningCall.id),
                        func.coalesce(func.sum(FactoryReasoningCall.input_tokens), 0),
                        func.coalesce(func.sum(FactoryReasoningCall.output_tokens), 0),
                        func.coalesce(func.sum(FactoryReasoningCall.runtime_ms), 0),
                        func.coalesce(func.sum(FactoryReasoningCall.cost_microusd), 0),
                    ).where(FactoryReasoningCall.run_id == rid)
                )
            ).one()
            event_count = int(
                (
                    await db.scalar(
                        select(func.count(FactoryEvent.id)).where(FactoryEvent.run_id == rid)
                    )
                )
                or 0
            )
            evidence_count = int(
                (
                    await db.scalar(
                        select(func.count(FactoryEvidence.id)).where(FactoryEvidence.run_id == rid)
                    )
                )
                or 0
            )

        required_gates = [row for row in gate_list if row["required"] and row["applicable"]]
        passed_required = [row for row in required_gates if row["status"] == "PASS"]
        failed_required = [row for row in required_gates if row["status"] == "FAIL"]
        active_workers = [
            row
            for row in worker_rows
            if row.status in {"ACTIVE", "CANCELLING", "QUIESCENT", "MISSING"}
        ]
        pending_approvals = [row for row in approval_rows if row.status == "PENDING"]
        selected_summary = _run_summary(selected, workspace_names.get(selected.workspace_id))
        selected_detail = {
            **selected_summary,
            "mission": selected.mission,
            "acceptance_criteria": list(selected.acceptance_criteria or []),
            "model_id": selected.model_id,
            "resumable_state": selected.resumable_state,
            "policy": _bounded_payload(selected.policy or {}),
            "budget": _bounded_payload(selected.budget or {}),
            "cycle": _cycle_dict(current_cycle) if current_cycle is not None else None,
            "cycles": [_cycle_dict(row) for row in cycles],
            "gates": gate_list,
            "gate_history": [_gate_dict(row) for row in gate_rows[:120]],
            "events": [_event_dict(row) for row in event_rows],
            "evidence": [_evidence_dict(row) for row in evidence_rows],
            "workers": [_worker_dict(row) for row in worker_rows],
            "reasoning": [_reasoning_dict(row) for row in reasoning_rows],
            "approvals": [_approval_dict(row) for row in approval_rows],
            "metrics": [_metric_dict(row) for row in metric_rows],
            "capability_outcomes": [
                _capability_outcome_dict(outcome, capability)
                for outcome, capability in capability_rows
            ],
            "commit_intents": [_commit_dict(row) for row in commit_rows],
            "ci_runs": [_ci_dict(row) for row in ci_rows],
            "progress": _progress_dict(selected, event_rows),
            "summary": {
                "cycle_count": len(cycles),
                "current_cycle_ordinal": int(current_cycle.ordinal) if current_cycle else 0,
                "event_count": event_count,
                "evidence_count": evidence_count,
                "required_gates": len(required_gates),
                "passed_required_gates": len(passed_required),
                "failed_required_gates": len(failed_required),
                "active_workers": len(active_workers),
                "pending_approvals": len(pending_approvals),
                "reasoning_calls": int(totals[0] or 0),
                "input_tokens": int(totals[1] or 0),
                "output_tokens": int(totals[2] or 0),
                "reasoning_runtime_ms": int(totals[3] or 0),
                "reasoning_cost_microusd": int(totals[4] or 0),
                "last_event_sequence": int(event_rows[-1].sequence) if event_rows else 0,
            },
        }
        core = {"version": 1, "runs": run_summaries, "selected": selected_detail}
        fingerprint = hashlib.sha256(
            json.dumps(core, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        ).hexdigest()
        return {**core, "fingerprint": fingerprint, "generated_at_ms": _now_ms()}
