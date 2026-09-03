"""Numeric-only observational metrics and proof-bound capability learning for Dark Factory."""

from __future__ import annotations

import time
import uuid
from collections import defaultdict
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from cptr.models import (
    FactoryCapabilityPerformance,
    FactoryCapabilityRecord,
    FactoryCycle,
    FactoryEvent,
    FactoryEvidence,
    FactoryGateResult,
    FactoryMetricProjection,
    FactoryReasoningCall,
    FactoryRun,
)
from cptr.services.factory_capabilities import (
    CapabilityManifest,
    CapabilityTrustStatus,
    CapabilityVerificationStatus,
)
from cptr.services.factory_capability_ranking import SqlCapabilityHistoryStore
from cptr.services.factory_domain import FactoryActor, FactoryState
from cptr.utils.db import get_session_factory

_MAX_DIMENSION_KEY = 500


def _now_ms() -> int:
    return int(time.time() * 1000)


def _family(value: str, label: str) -> str:
    normalized = "-".join(str(value).strip().lower().split())
    if not normalized:
        raise ValueError(f"{label} must not be blank")
    if len(normalized) > 160:
        raise ValueError(f"{label} exceeds bounded length")
    return normalized


def _dimension(value: object) -> str:
    text = str(value or "").strip()
    if len(text) > _MAX_DIMENSION_KEY:
        raise ValueError("factory metric dimension exceeds bounded length")
    return text


def _nonnegative_int(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)


def _metric_id(run_id: str, cycle_id: str | None, scope: str, dimension_key: str) -> str:
    stable = f"{run_id}|{cycle_id or '-'}|{scope}|{dimension_key}"
    return "fmetric_" + uuid.uuid5(uuid.NAMESPACE_URL, "cptr-factory-metric:" + stable).hex


def _selected_capability_ids(value: Iterable[object]) -> tuple[str, ...]:
    identities: list[str] = []
    for item in value:
        identity = ""
        if isinstance(item, str):
            identity = item.strip()
        elif isinstance(item, dict):
            for key in ("identity", "capability_id", "id"):
                candidate = item.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    identity = candidate.strip()
                    break
        if identity and identity not in identities:
            identities.append(identity)
    return tuple(identities)


def _manifest_from_record(row: FactoryCapabilityRecord) -> CapabilityManifest:
    historical = (
        int(row.historical_factory_score_ppm) / 1_000_000
        if row.historical_factory_score_ppm is not None
        else None
    )
    return CapabilityManifest(
        stable_id=str(row.stable_id),
        version=str(row.version),
        origin_type=str(row.origin_type),
        origin_uri=str(row.origin_uri),
        pinned_version_or_commit=(
            str(row.pinned_version_or_commit) if row.pinned_version_or_commit is not None else None
        ),
        digest=str(row.digest),
        capabilities=tuple(str(item) for item in row.capabilities or ()),
        permissions=tuple(str(item) for item in row.permissions or ()),
        network_requirements=tuple(str(item) for item in row.network_requirements or ()),
        execution_requirements=tuple(str(item) for item in row.execution_requirements or ()),
        risk_classification=str(row.risk_classification),
        trust_status=CapabilityTrustStatus(str(row.trust_status)),
        verification_status=CapabilityVerificationStatus(str(row.verification_status)),
        maintenance_metadata=dict(row.maintenance_metadata or {}),
        historical_factory_score=historical,
        created_at=int(row.created_at),
        evaluated_at=int(row.evaluated_at) if row.evaluated_at is not None else None,
    )


def _execution_metric(payload: object) -> tuple[str, dict[str, int]] | None:
    if not isinstance(payload, dict):
        return None
    identity = payload.get("capability_identity")
    if not isinstance(identity, str) or not identity.strip():
        return None
    return identity.strip(), {
        "attempts": 1,
        "input_tokens": _nonnegative_int(payload.get("input_tokens")),
        "output_tokens": _nonnegative_int(payload.get("output_tokens")),
        "runtime_ms": _nonnegative_int(payload.get("runtime_ms")),
        "cost_microusd": _nonnegative_int(payload.get("cost_microusd")),
    }


def _sum_metric(rows: Iterable[dict[str, Any]]) -> dict[str, int]:
    totals = {
        "attempts": 0,
        "repair_iterations": 0,
        "regressions": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "runtime_ms": 0,
        "cost_microusd": 0,
        "gate_latency_ms": 0,
    }
    for row in rows:
        for key in totals:
            totals[key] += _nonnegative_int(row.get(key))
    return totals


class FactoryMetricsService:
    """Build durable observed-work projections without persisting model/source payloads."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker | None = None,
        history_store: SqlCapabilityHistoryStore | None = None,
    ) -> None:
        self._session_factory = session_factory or get_session_factory()
        self._history = history_store or SqlCapabilityHistoryStore(
            session_factory=self._session_factory
        )

    async def refresh_run(
        self,
        run_id: str,
        *,
        repository_family: str,
        task_family: str,
    ) -> dict[str, Any]:
        repository_family = _family(repository_family, "repository family")
        task_family = _family(task_family, "task family")

        async with self._session_factory() as db:
            run = await db.get(FactoryRun, run_id)
            if run is None:
                raise KeyError("factory run not found")
            cycles = list(
                (
                    await db.scalars(
                        select(FactoryCycle)
                        .where(FactoryCycle.run_id == run_id)
                        .order_by(FactoryCycle.ordinal.asc())
                    )
                ).all()
            )
            reasoning = list(
                (
                    await db.scalars(
                        select(FactoryReasoningCall).where(FactoryReasoningCall.run_id == run_id)
                    )
                ).all()
            )
            gates = list(
                (
                    await db.scalars(
                        select(FactoryGateResult).where(FactoryGateResult.run_id == run_id)
                    )
                ).all()
            )
            evidence = list(
                (
                    await db.scalars(
                        select(FactoryEvidence).where(FactoryEvidence.run_id == run_id)
                    )
                ).all()
            )
            events = list(
                (
                    await db.scalars(
                        select(FactoryEvent).where(FactoryEvent.run_id == run_id)
                    )
                ).all()
            )

            selected_ids = {
                capability_id
                for cycle in cycles
                for capability_id in _selected_capability_ids(cycle.selected_capabilities or ())
            }
            capability_records: dict[str, FactoryCapabilityRecord] = {}
            if selected_ids:
                rows = list(
                    (
                        await db.scalars(
                            select(FactoryCapabilityRecord).where(
                                FactoryCapabilityRecord.id.in_(sorted(selected_ids))
                            )
                        )
                    ).all()
                )
                capability_records = {str(row.id): row for row in rows}

        victories = {
            str(event.cycle_id)
            for event in events
            if event.cycle_id
            and event.event_type == "victory.authorized"
            and event.actor == FactoryActor.SYSTEM.value
        }
        run_outcome: str | None = None
        if run.state == FactoryState.COMPLETE.value and run.current_cycle_id in victories:
            run_outcome = "SUCCESS"
        elif run.state in {FactoryState.BLOCKED.value, FactoryState.FAILED.value}:
            run_outcome = "FAILURE"

        reasoning_by_cycle: dict[str, list[FactoryReasoningCall]] = defaultdict(list)
        for row in reasoning:
            reasoning_by_cycle[str(row.cycle_id)].append(row)
        gates_by_cycle: dict[str, list[FactoryGateResult]] = defaultdict(list)
        for row in gates:
            gates_by_cycle[str(row.cycle_id)].append(row)
        evidence_by_cycle: dict[str, list[FactoryEvidence]] = defaultdict(list)
        for row in evidence:
            if row.cycle_id:
                evidence_by_cycle[str(row.cycle_id)].append(row)

        projection_rows: list[dict[str, Any]] = []
        cycle_summaries: list[dict[str, Any]] = []
        role_summaries: list[dict[str, Any]] = []
        gate_summaries: list[dict[str, Any]] = []
        capability_summaries: list[dict[str, Any]] = []

        for cycle in cycles:
            cycle_id = str(cycle.id)
            cycle_reasoning = reasoning_by_cycle.get(cycle_id, [])
            cycle_gates = gates_by_cycle.get(cycle_id, [])
            cycle_evidence = evidence_by_cycle.get(cycle_id, [])
            regression_count = sum(1 for gate in cycle_gates if gate.status == "FAIL")
            repair_iterations = int(cycle.attempt_count or 0)
            cycle_outcome = (
                "SUCCESS"
                if run_outcome == "SUCCESS" and cycle_id in victories
                else "FAILURE"
                if run_outcome == "FAILURE" and run.current_cycle_id == cycle_id
                else None
            )

            execution_by_capability: dict[str, dict[str, int]] = defaultdict(
                lambda: {
                    "attempts": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "runtime_ms": 0,
                    "cost_microusd": 0,
                }
            )
            for item in cycle_evidence:
                parsed = _execution_metric(item.payload)
                if parsed is None:
                    continue
                identity, values = parsed
                target = execution_by_capability[identity]
                for key, value in values.items():
                    target[key] += value

            role_groups: dict[str, list[FactoryReasoningCall]] = defaultdict(list)
            for row in cycle_reasoning:
                role_groups[str(row.role)].append(row)
            role_metrics: list[dict[str, Any]] = []
            for role, rows in sorted(role_groups.items()):
                metric = {
                    "run_id": run_id,
                    "cycle_id": cycle_id,
                    "scope": "role",
                    "dimension_key": _dimension(role),
                    "attempts": sum(int(row.attempt_count or 0) for row in rows),
                    "repair_iterations": 0,
                    "regressions": 0,
                    "input_tokens": sum(int(row.input_tokens or 0) for row in rows),
                    "output_tokens": sum(int(row.output_tokens or 0) for row in rows),
                    "runtime_ms": sum(int(row.runtime_ms or 0) for row in rows),
                    "cost_microusd": sum(int(row.cost_microusd or 0) for row in rows),
                    "gate_latency_ms": 0,
                    "verified_outcome": cycle_outcome,
                }
                role_metrics.append(metric)
                role_summaries.append({
                    "cycle_id": cycle_id,
                    "role": role,
                    **{key: metric[key] for key in (
                        "attempts", "input_tokens", "output_tokens", "runtime_ms", "cost_microusd"
                    )},
                    "verified_outcome": cycle_outcome,
                })
                projection_rows.append(metric)

            latest_gates: dict[str, FactoryGateResult] = {}
            for gate in cycle_gates:
                current = latest_gates.get(str(gate.gate_id))
                if current is None or int(gate.attempt or 0) > int(current.attempt or 0):
                    latest_gates[str(gate.gate_id)] = gate
            for gate_id, gate in sorted(latest_gates.items()):
                latency = max(0, int(gate.updated_at or 0) - int(cycle.created_at or 0))
                metric = {
                    "run_id": run_id,
                    "cycle_id": cycle_id,
                    "scope": "gate",
                    "dimension_key": _dimension(gate_id),
                    "attempts": int(gate.attempt or 0),
                    "repair_iterations": 0,
                    "regressions": 1 if gate.status == "FAIL" else 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "runtime_ms": 0,
                    "cost_microusd": 0,
                    "gate_latency_ms": latency,
                    "verified_outcome": (
                        "SUCCESS" if gate.status == "PASS" and cycle_outcome == "SUCCESS" else None
                    ),
                }
                gate_summaries.append({
                    "cycle_id": cycle_id,
                    "gate_id": gate_id,
                    "attempts": metric["attempts"],
                    "regressions": metric["regressions"],
                    "gate_latency_ms": latency,
                    "verified_outcome": metric["verified_outcome"],
                })
                projection_rows.append(metric)

            capability_metrics: list[dict[str, Any]] = []
            selected = _selected_capability_ids(cycle.selected_capabilities or ())
            for identity in selected:
                observed = execution_by_capability.get(identity) or {
                    "attempts": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "runtime_ms": 0,
                    "cost_microusd": 0,
                }
                metric = {
                    "run_id": run_id,
                    "cycle_id": cycle_id,
                    "scope": "capability",
                    "dimension_key": _dimension(identity),
                    "attempts": max(1, int(observed["attempts"])),
                    "repair_iterations": repair_iterations,
                    "regressions": regression_count,
                    "input_tokens": int(observed["input_tokens"]),
                    "output_tokens": int(observed["output_tokens"]),
                    "runtime_ms": int(observed["runtime_ms"]),
                    "cost_microusd": int(observed["cost_microusd"]),
                    "gate_latency_ms": 0,
                    "verified_outcome": cycle_outcome,
                }
                capability_metrics.append(metric)
                capability_summaries.append({
                    "cycle_id": cycle_id,
                    "capability_id": identity,
                    **{key: metric[key] for key in (
                        "attempts", "repair_iterations", "regressions", "input_tokens",
                        "output_tokens", "runtime_ms", "cost_microusd"
                    )},
                    "verified_outcome": cycle_outcome,
                })
                projection_rows.append(metric)

            reasoning_totals = _sum_metric(role_metrics)
            capability_execution_totals = {
                "attempts": sum(item["attempts"] for item in execution_by_capability.values()),
                "input_tokens": sum(item["input_tokens"] for item in execution_by_capability.values()),
                "output_tokens": sum(item["output_tokens"] for item in execution_by_capability.values()),
                "runtime_ms": sum(item["runtime_ms"] for item in execution_by_capability.values()),
                "cost_microusd": sum(item["cost_microusd"] for item in execution_by_capability.values()),
            }
            cycle_metric = {
                "run_id": run_id,
                "cycle_id": cycle_id,
                "scope": "cycle",
                "dimension_key": str(int(cycle.ordinal)),
                "attempts": max(
                    int(cycle.attempt_count or 0),
                    int(reasoning_totals["attempts"]),
                    int(capability_execution_totals["attempts"]),
                ),
                "repair_iterations": repair_iterations,
                "regressions": regression_count,
                "input_tokens": reasoning_totals["input_tokens"] + capability_execution_totals["input_tokens"],
                "output_tokens": reasoning_totals["output_tokens"] + capability_execution_totals["output_tokens"],
                "runtime_ms": reasoning_totals["runtime_ms"] + capability_execution_totals["runtime_ms"],
                "cost_microusd": reasoning_totals["cost_microusd"] + capability_execution_totals["cost_microusd"],
                "gate_latency_ms": sum(
                    max(0, int(gate.updated_at or 0) - int(cycle.created_at or 0))
                    for gate in latest_gates.values()
                ),
                "verified_outcome": cycle_outcome,
            }
            cycle_summaries.append({
                "cycle_id": cycle_id,
                "ordinal": int(cycle.ordinal),
                **{key: cycle_metric[key] for key in (
                    "attempts", "repair_iterations", "regressions", "input_tokens", "output_tokens",
                    "runtime_ms", "cost_microusd", "gate_latency_ms"
                )},
                "verified_outcome": cycle_outcome,
            })
            projection_rows.append(cycle_metric)

        run_totals = _sum_metric(cycle_summaries)
        run_metric = {
            "run_id": run_id,
            "cycle_id": None,
            "scope": "run",
            "dimension_key": "",
            **run_totals,
            "verified_outcome": run_outcome,
        }
        projection_rows.append(run_metric)
        await self._persist_projections(projection_rows)

        if run_outcome == "SUCCESS":
            for metric in capability_summaries:
                if metric["verified_outcome"] != "SUCCESS":
                    continue
                record = capability_records.get(str(metric["capability_id"]))
                if record is None:
                    continue
                await self._history.record_capability_outcome(
                    manifest=_manifest_from_record(record),
                    run_id=run_id,
                    cycle_id=str(metric["cycle_id"]),
                    repository_family=repository_family,
                    task_family=task_family,
                    verified_success=True,
                    regression=bool(metric["regressions"]),
                    repair_iterations=int(metric["repair_iterations"]),
                    input_tokens=int(metric["input_tokens"]),
                    output_tokens=int(metric["output_tokens"]),
                    runtime_ms=int(metric["runtime_ms"]),
                    cost_usd=int(metric["cost_microusd"]) / 1_000_000,
                )

        return {
            "run_id": run_id,
            "comparable": False,
            "comparability": "observed_real_work_only",
            "run": {
                **{key: run_metric[key] for key in (
                    "attempts", "repair_iterations", "regressions", "input_tokens", "output_tokens",
                    "runtime_ms", "cost_microusd", "gate_latency_ms"
                )},
                "verified_outcome": run_outcome,
            },
            "cycles": cycle_summaries,
            "roles": role_summaries,
            "capabilities": capability_summaries,
            "gates": gate_summaries,
        }

    async def _persist_projections(self, rows: Iterable[dict[str, Any]]) -> None:
        now = _now_ms()
        table = FactoryMetricProjection.__table__
        async with self._session_factory() as db:
            async with db.begin():
                for row in rows:
                    values = {
                        "id": _metric_id(
                            str(row["run_id"]),
                            str(row["cycle_id"]) if row.get("cycle_id") else None,
                            str(row["scope"]),
                            str(row.get("dimension_key") or ""),
                        ),
                        "run_id": str(row["run_id"]),
                        "cycle_id": str(row["cycle_id"]) if row.get("cycle_id") else None,
                        "scope": str(row["scope"]),
                        "dimension_key": _dimension(row.get("dimension_key")),
                        "attempts": _nonnegative_int(row.get("attempts")),
                        "repair_iterations": _nonnegative_int(row.get("repair_iterations")),
                        "regressions": _nonnegative_int(row.get("regressions")),
                        "input_tokens": _nonnegative_int(row.get("input_tokens")),
                        "output_tokens": _nonnegative_int(row.get("output_tokens")),
                        "runtime_ms": _nonnegative_int(row.get("runtime_ms")),
                        "cost_microusd": _nonnegative_int(row.get("cost_microusd")),
                        "gate_latency_ms": _nonnegative_int(row.get("gate_latency_ms")),
                        "verified_outcome": row.get("verified_outcome"),
                        "updated_at": now,
                    }
                    insert = sqlite_insert(table).values(**values)
                    excluded = insert.excluded
                    await db.execute(
                        insert.on_conflict_do_update(
                            index_elements=[table.c.id],
                            set_={
                                "attempts": excluded.attempts,
                                "repair_iterations": excluded.repair_iterations,
                                "regressions": excluded.regressions,
                                "input_tokens": excluded.input_tokens,
                                "output_tokens": excluded.output_tokens,
                                "runtime_ms": excluded.runtime_ms,
                                "cost_microusd": excluded.cost_microusd,
                                "gate_latency_ms": excluded.gate_latency_ms,
                                "verified_outcome": excluded.verified_outcome,
                                "updated_at": excluded.updated_at,
                            },
                        )
                    )

    async def longitudinal_summary(
        self,
        *,
        repository_family: str,
        task_family: str,
    ) -> dict[str, Any]:
        repository_family = _family(repository_family, "repository family")
        task_family = _family(task_family, "task family")
        async with self._session_factory() as db:
            rows = list(
                (
                    await db.scalars(
                        select(FactoryCapabilityPerformance).where(
                            FactoryCapabilityPerformance.repository_family == repository_family,
                            FactoryCapabilityPerformance.task_family == task_family,
                        )
                    )
                ).all()
            )
        capabilities = []
        for row in rows:
            attempts = int(row.attempts or 0)
            verified = int(row.verified_successes or 0) + int(row.verified_failures or 0)
            capabilities.append(
                {
                    "capability_id": str(row.capability_id),
                    "attempts": attempts,
                    "verified_successes": int(row.verified_successes or 0),
                    "verified_failures": int(row.verified_failures or 0),
                    "verified_success_rate": round(
                        int(row.verified_successes or 0) / verified, 6
                    ) if verified else None,
                    "regressions": int(row.regressions or 0),
                    "regression_rate": round(int(row.regressions or 0) / attempts, 6)
                    if attempts else 0.0,
                    "repair_iterations": int(row.repair_iterations or 0),
                    "input_tokens": int(row.input_tokens or 0),
                    "output_tokens": int(row.output_tokens or 0),
                    "runtime_ms": int(row.runtime_ms or 0),
                    "cost_microusd": int(row.cost_microusd or 0),
                    "confidence": round(min(1.0, attempts / 10.0), 6),
                }
            )
        capabilities.sort(
            key=lambda item: (
                -(item["verified_success_rate"] if item["verified_success_rate"] is not None else -1),
                item["regression_rate"],
                item["capability_id"],
            )
        )
        return {
            "comparable": False,
            "comparability": "observed_real_work_only",
            "capabilities": capabilities,
        }
