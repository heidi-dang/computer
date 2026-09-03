"""Trust-gated ranking and durable performance memory for factory capabilities."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any, Iterable

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from cptr.models import (
    FactoryCapabilityOutcome,
    FactoryCapabilityPerformance,
    FactoryCapabilityRecord,
    FactoryCycle,
    FactoryEvent,
    FactoryRun,
)
from cptr.services.factory_domain import FactoryActor, FactoryState
from cptr.services.factory_capabilities import (
    CapabilityManifest,
    CapabilityRequirement,
    CapabilityTrustStatus,
    CapabilityVerificationStatus,
)
from cptr.utils.db import get_session_factory


@dataclass(frozen=True)
class CapabilityHistory:
    attempts: int = 0
    verified_successes: int = 0
    verified_failures: int = 0
    regressions: int = 0
    repair_iterations: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    runtime_ms: int = 0
    cost_usd: float = 0.0

    def __post_init__(self) -> None:
        values = (
            self.attempts,
            self.verified_successes,
            self.verified_failures,
            self.regressions,
            self.repair_iterations,
            self.input_tokens,
            self.output_tokens,
            self.runtime_ms,
        )
        if any(value < 0 for value in values) or self.cost_usd < 0:
            raise ValueError("capability history metrics must not be negative")
        if self.verified_successes + self.verified_failures > self.attempts:
            raise ValueError("verified capability outcomes cannot exceed attempts")

    @property
    def confidence(self) -> float:
        # Ten independently verified outcomes are enough to fully weight history.
        return min(1.0, self.attempts / 10.0)

    @property
    def verified_success_rate(self) -> float:
        verified = self.verified_successes + self.verified_failures
        if verified <= 0:
            return 0.5
        return self.verified_successes / verified

    @property
    def objective_quality(self) -> float:
        if self.attempts <= 0:
            return 0.5
        regression_penalty = min(0.35, self.regressions / self.attempts * 0.35)
        repair_penalty = min(0.2, self.repair_iterations / self.attempts * 0.05)
        return _clamp(self.verified_success_rate - regression_penalty - repair_penalty)

    @classmethod
    def perfect(cls, *, attempts: int) -> "CapabilityHistory":
        return cls(attempts=attempts, verified_successes=attempts)


@dataclass(frozen=True)
class CapabilityRankingPolicy:
    allowed_permissions: frozenset[str]
    network_allowed: bool

    def __post_init__(self) -> None:
        if any(not str(value).strip() for value in self.allowed_permissions):
            raise ValueError("allowed capability permissions must not be blank")


@dataclass(frozen=True)
class RankedCapability:
    manifest: CapabilityManifest
    total_score: float
    components: dict[str, float]
    history_confidence: float


_ELIGIBLE_TRUST = {CapabilityTrustStatus.APPROVED}
_VERIFICATION_QUALITY = {
    CapabilityVerificationStatus.UNVERIFIED: 0.45,
    CapabilityVerificationStatus.LOCAL: 0.8,
    CapabilityVerificationStatus.STATIC_VERIFIED: 0.9,
    CapabilityVerificationStatus.CAPABILITY_TESTED: 1.0,
}


def rank_capabilities(
    requirements: Iterable[CapabilityRequirement],
    candidates: Iterable[CapabilityManifest],
    history: dict[str, CapabilityHistory],
    policy: CapabilityRankingPolicy,
) -> list[RankedCapability]:
    """Filter on trust/permissions first, then rank with decomposed objective scores."""

    requirement_list = tuple(requirements)
    if not requirement_list:
        return []
    ranked: list[RankedCapability] = []
    for candidate in candidates:
        if not _eligible(candidate, policy):
            continue
        fit = _fit_score(requirement_list, candidate)
        if fit <= 0:
            continue
        item_history = history.get(candidate.identity, CapabilityHistory())
        history_confidence = item_history.confidence
        history_score = item_history.objective_quality * history_confidence + 0.5 * (
            1.0 - history_confidence
        )
        maintenance = _metadata_score(candidate.maintenance_metadata, "maintenance_score", 0.5)
        freshness = _metadata_score(candidate.maintenance_metadata, "freshness_score", 0.5)
        quality = _VERIFICATION_QUALITY[candidate.verification_status]
        latency = _latency_score(item_history)
        token_efficiency = _token_score(item_history)
        cost = _cost_score(item_history)
        components = {
            "fit": fit,
            "quality": quality,
            "history": history_score,
            "maintenance": maintenance,
            "freshness": freshness,
            "latency": latency,
            "token_efficiency": token_efficiency,
            "cost": cost,
        }
        total = (
            fit * 0.38
            + quality * 0.14
            + history_score * 0.20
            + maintenance * 0.08
            + freshness * 0.08
            + latency * 0.04
            + token_efficiency * 0.04
            + cost * 0.04
        )
        ranked.append(
            RankedCapability(
                manifest=candidate,
                total_score=round(total, 12),
                components={key: round(value, 12) for key, value in components.items()},
                history_confidence=round(history_confidence, 12),
            )
        )

    ranked.sort(key=lambda item: (-item.total_score, item.manifest.identity))
    return ranked


def _eligible(candidate: CapabilityManifest, policy: CapabilityRankingPolicy) -> bool:
    if candidate.trust_status not in _ELIGIBLE_TRUST:
        return False
    if not set(candidate.permissions).issubset(policy.allowed_permissions):
        return False
    if candidate.network_requirements and not policy.network_allowed:
        return False
    return True


def _fit_score(
    requirements: tuple[CapabilityRequirement, ...],
    candidate: CapabilityManifest,
) -> float:
    candidate_capabilities = set(candidate.capabilities)
    candidate_permissions = set(candidate.permissions)
    scores: list[float] = []
    for requirement in requirements:
        if candidate.network_requirements and not requirement.network_allowed:
            scores.append(0.0)
            continue
        if not set(requirement.required_permissions).issubset(candidate_permissions):
            scores.append(0.0)
            continue
        required = set(requirement.capabilities)
        scores.append(len(required & candidate_capabilities) / len(required))
    return sum(scores) / len(scores)


def _metadata_score(metadata: dict[str, Any], key: str, default: float) -> float:
    value = metadata.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return _clamp(float(value))


def _latency_score(history: CapabilityHistory) -> float:
    if history.attempts <= 0:
        return 0.5
    average_ms = history.runtime_ms / history.attempts
    return 1.0 / (1.0 + average_ms / 10_000.0)


def _token_score(history: CapabilityHistory) -> float:
    if history.attempts <= 0:
        return 0.5
    average_tokens = (history.input_tokens + history.output_tokens) / history.attempts
    return 1.0 / (1.0 + average_tokens / 20_000.0)


def _cost_score(history: CapabilityHistory) -> float:
    if history.attempts <= 0:
        return 0.5
    average_cost = history.cost_usd / history.attempts
    return 1.0 / (1.0 + average_cost / 0.25)


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _normalize_family(value: str, label: str) -> str:
    normalized = value.strip().lower()
    if not normalized:
        raise ValueError(f"{label} must not be blank")
    if len(normalized) > 160:
        raise ValueError(f"{label} exceeds bounded length")
    return normalized


def _now_ms() -> int:
    return int(time.time() * 1000)


class SqlCapabilityHistoryStore:
    """Persist immutable manifests and objective outcomes from machine-verified runs only."""

    def __init__(self, *, session_factory: async_sessionmaker | None = None) -> None:
        self._session_factory = session_factory or get_session_factory()

    async def record_capability_outcome(
        self,
        *,
        manifest: CapabilityManifest,
        run_id: str,
        cycle_id: str,
        repository_family: str,
        task_family: str,
        verified_success: bool,
        regression: bool,
        repair_iterations: int,
        input_tokens: int,
        output_tokens: int,
        runtime_ms: int,
        cost_usd: float,
    ) -> CapabilityHistory:
        """Record one idempotent capability outcome after verifying durable machine proof.

        A caller cannot turn a Boolean into success authority. Successful learning
        requires a COMPLETE factory run plus the server-owned ``victory.authorized``
        event for the same cycle. Verified failures require a terminal BLOCKED/FAILED
        run plus a persisted machine ``failure.recorded`` event.
        """

        metrics = (repair_iterations, input_tokens, output_tokens, runtime_ms)
        if any(value < 0 for value in metrics) or cost_usd < 0:
            raise ValueError("capability outcome metrics must not be negative")
        repository_family = _normalize_family(repository_family, "repository family")
        task_family = _normalize_family(task_family, "task family")
        now = _now_ms()
        cost_microusd = int(round(cost_usd * 1_000_000))
        outcome_id = (
            "fcapout_"
            + uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"cptr-factory:{run_id}:{cycle_id}:{manifest.identity}",
            ).hex
        )

        async with self._session_factory() as db:
            async with db.begin():
                run = await db.get(FactoryRun, run_id)
                cycle = await db.get(FactoryCycle, cycle_id)
                if run is None or cycle is None or cycle.run_id != run_id:
                    raise ValueError("capability outcome requires an existing factory run/cycle")

                proof_type = "victory.authorized" if verified_success else "failure.recorded"
                allowed_states = (
                    {FactoryState.COMPLETE.value}
                    if verified_success
                    else {FactoryState.BLOCKED.value, FactoryState.FAILED.value}
                )
                if run.state not in allowed_states:
                    raise ValueError(
                        "capability outcomes require machine-verified terminal factory evidence"
                    )
                proof = (
                    await db.execute(
                        select(FactoryEvent)
                        .where(
                            FactoryEvent.run_id == run_id,
                            FactoryEvent.cycle_id == cycle_id,
                            FactoryEvent.event_type == proof_type,
                            FactoryEvent.actor == FactoryActor.SYSTEM.value,
                        )
                        .order_by(FactoryEvent.sequence.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if proof is None:
                    raise ValueError(
                        "capability outcomes require machine-verified factory evidence"
                    )

                existing_outcome = await db.get(FactoryCapabilityOutcome, outcome_id)
                if existing_outcome is not None:
                    expected = (
                        repository_family,
                        task_family,
                        bool(verified_success),
                        proof.id,
                        bool(regression),
                        repair_iterations,
                        input_tokens,
                        output_tokens,
                        runtime_ms,
                        cost_microusd,
                    )
                    observed = (
                        existing_outcome.repository_family,
                        existing_outcome.task_family,
                        bool(existing_outcome.verified_success),
                        existing_outcome.proof_event_id,
                        bool(existing_outcome.regression),
                        int(existing_outcome.repair_iterations),
                        int(existing_outcome.input_tokens),
                        int(existing_outcome.output_tokens),
                        int(existing_outcome.runtime_ms),
                        int(existing_outcome.cost_microusd),
                    )
                    if observed != expected:
                        raise ValueError(
                            "capability outcome replay changed immutable verified metrics"
                        )
                    result = await db.execute(
                        select(FactoryCapabilityPerformance).where(
                            FactoryCapabilityPerformance.capability_id == manifest.identity,
                            FactoryCapabilityPerformance.repository_family == repository_family,
                            FactoryCapabilityPerformance.task_family == task_family,
                        )
                    )
                    row = result.scalar_one()
                    return self._history_from_row(row)

                await self._upsert_manifest(db, manifest, now=now)
                table = FactoryCapabilityPerformance.__table__
                insert = sqlite_insert(table).values(
                    id=f"fcapperf_{uuid.uuid4().hex}",
                    capability_id=manifest.identity,
                    repository_family=repository_family,
                    task_family=task_family,
                    attempts=1,
                    verified_successes=1 if verified_success else 0,
                    verified_failures=0 if verified_success else 1,
                    regressions=1 if regression else 0,
                    repair_iterations=repair_iterations,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    runtime_ms=runtime_ms,
                    cost_microusd=cost_microusd,
                    confidence_ppm=100_000,
                    created_at=now,
                    updated_at=now,
                )
                excluded = insert.excluded
                statement = insert.on_conflict_do_update(
                    index_elements=[
                        table.c.capability_id,
                        table.c.repository_family,
                        table.c.task_family,
                    ],
                    set_={
                        "attempts": table.c.attempts + 1,
                        "verified_successes": table.c.verified_successes
                        + excluded.verified_successes,
                        "verified_failures": table.c.verified_failures + excluded.verified_failures,
                        "regressions": table.c.regressions + excluded.regressions,
                        "repair_iterations": table.c.repair_iterations + excluded.repair_iterations,
                        "input_tokens": table.c.input_tokens + excluded.input_tokens,
                        "output_tokens": table.c.output_tokens + excluded.output_tokens,
                        "runtime_ms": table.c.runtime_ms + excluded.runtime_ms,
                        "cost_microusd": table.c.cost_microusd + excluded.cost_microusd,
                        "confidence_ppm": (table.c.attempts + 1) * 100_000,
                        "updated_at": now,
                    },
                )
                await db.execute(statement)
                db.add(
                    FactoryCapabilityOutcome(
                        id=outcome_id,
                        run_id=run_id,
                        cycle_id=cycle_id,
                        capability_id=manifest.identity,
                        repository_family=repository_family,
                        task_family=task_family,
                        verified_success=verified_success,
                        proof_event_id=proof.id,
                        regression=regression,
                        repair_iterations=repair_iterations,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        runtime_ms=runtime_ms,
                        cost_microusd=cost_microusd,
                        created_at=now,
                    )
                )

                result = await db.execute(
                    select(FactoryCapabilityPerformance).where(
                        FactoryCapabilityPerformance.capability_id == manifest.identity,
                        FactoryCapabilityPerformance.repository_family == repository_family,
                        FactoryCapabilityPerformance.task_family == task_family,
                    )
                )
                row = result.scalar_one()
                if row.confidence_ppm > 1_000_000:
                    row.confidence_ppm = 1_000_000
                history = self._history_from_row(row)
                score_ppm = int(round(history.objective_quality * 1_000_000))
                await db.execute(
                    update(FactoryCapabilityRecord)
                    .where(FactoryCapabilityRecord.id == manifest.identity)
                    .values(historical_factory_score_ppm=score_ppm, evaluated_at=now)
                )
                return history

    async def get_history(
        self,
        capability_identity: str,
        *,
        repository_family: str,
        task_family: str,
    ) -> CapabilityHistory | None:
        repository_family = _normalize_family(repository_family, "repository family")
        task_family = _normalize_family(task_family, "task family")
        async with self._session_factory() as db:
            result = await db.execute(
                select(FactoryCapabilityPerformance).where(
                    FactoryCapabilityPerformance.capability_id == capability_identity,
                    FactoryCapabilityPerformance.repository_family == repository_family,
                    FactoryCapabilityPerformance.task_family == task_family,
                )
            )
            row = result.scalar_one_or_none()
        return self._history_from_row(row) if row is not None else None

    async def _upsert_manifest(self, db: Any, manifest: CapabilityManifest, *, now: int) -> None:
        table = FactoryCapabilityRecord.__table__
        statement = (
            sqlite_insert(table)
            .values(
                id=manifest.identity,
                stable_id=manifest.stable_id,
                version=manifest.version,
                origin_type=manifest.origin_type,
                origin_uri=manifest.origin_uri,
                pinned_version_or_commit=manifest.pinned_version_or_commit,
                digest=manifest.digest,
                capabilities=list(manifest.capabilities),
                permissions=list(manifest.permissions),
                network_requirements=list(manifest.network_requirements),
                execution_requirements=list(manifest.execution_requirements),
                risk_classification=manifest.risk_classification,
                trust_status=manifest.trust_status.value,
                verification_status=manifest.verification_status.value,
                maintenance_metadata=manifest.maintenance_metadata,
                historical_factory_score_ppm=(
                    int(round(manifest.historical_factory_score * 1_000_000))
                    if manifest.historical_factory_score is not None
                    else None
                ),
                created_at=manifest.created_at or now,
                evaluated_at=manifest.evaluated_at,
            )
            .on_conflict_do_update(
                index_elements=[table.c.id],
                set_={
                    "trust_status": manifest.trust_status.value,
                    "verification_status": manifest.verification_status.value,
                    "maintenance_metadata": manifest.maintenance_metadata,
                    "evaluated_at": manifest.evaluated_at or now,
                },
            )
        )
        await db.execute(statement)

    @staticmethod
    def _history_from_row(row: FactoryCapabilityPerformance) -> CapabilityHistory:
        return CapabilityHistory(
            attempts=int(row.attempts),
            verified_successes=int(row.verified_successes),
            verified_failures=int(row.verified_failures),
            regressions=int(row.regressions),
            repair_iterations=int(row.repair_iterations),
            input_tokens=int(row.input_tokens),
            output_tokens=int(row.output_tokens),
            runtime_ms=int(row.runtime_ms),
            cost_usd=int(row.cost_microusd) / 1_000_000,
        )
