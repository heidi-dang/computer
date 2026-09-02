"""Single-observation, revision-bound CI tracking for Dark Factory cycles."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Mapping, Protocol

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from cptr.models import FactoryCiRun, FactoryCycle, FactoryRun
from cptr.utils.db import get_session_factory
from cptr.utils.redaction import redact_text


class FactoryCiError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class CiObservation:
    status: str
    conclusion: str | None = None
    url: str | None = None
    failure_summary: str | None = None

    def __post_init__(self) -> None:
        if not self.status.strip():
            raise ValueError("CI observation status must not be blank")
        if self.url is not None and len(self.url) > 4_096:
            raise ValueError("CI observation URL exceeds bounded length")
        if self.failure_summary is not None and len(self.failure_summary) > 8_000:
            raise ValueError("CI failure summary exceeds bounded length")


@dataclass(frozen=True)
class CiPollRequest:
    provider: str
    repository: str
    revision: str
    external_run_id: str
    check_id: str


class CiProvider(Protocol):
    async def observe(self, request: CiPollRequest) -> CiObservation: ...


def _now_ms() -> int:
    return int(time.time() * 1000)


def _token(value: str, label: str, max_length: int = 500) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{label} must not be blank")
    if len(normalized) > max_length:
        raise ValueError(f"{label} exceeds bounded length")
    return normalized


def _status(value: str) -> str:
    normalized = value.strip().upper().replace("-", "_")
    aliases = {
        "IN_PROGRESS": "IN_PROGRESS",
        "RUNNING": "IN_PROGRESS",
        "QUEUED": "QUEUED",
        "PENDING": "QUEUED",
        "COMPLETED": "COMPLETED",
        "COMPLETE": "COMPLETED",
    }
    if normalized not in aliases:
        raise FactoryCiError("FACTORY_CI_INVALID_STATUS", f"unsupported CI status {value!r}")
    return aliases[normalized]


def _conclusion(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    normalized = value.strip().upper().replace("-", "_")
    aliases = {
        "SUCCESS": "SUCCESS",
        "PASSED": "SUCCESS",
        "FAILURE": "FAILURE",
        "FAILED": "FAILURE",
        "CANCELLED": "CANCELLED",
        "CANCELED": "CANCELLED",
        "TIMED_OUT": "TIMED_OUT",
        "TIMEOUT": "TIMED_OUT",
        "SKIPPED": "SKIPPED",
        "NEUTRAL": "NEUTRAL",
        "ACTION_REQUIRED": "ACTION_REQUIRED",
    }
    if normalized not in aliases:
        raise FactoryCiError(
            "FACTORY_CI_INVALID_CONCLUSION", f"unsupported CI conclusion {value!r}"
        )
    return aliases[normalized]


_FAILURE_CONCLUSIONS = {"FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED"}


class FactoryCiService:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker | None = None,
        providers: Mapping[str, CiProvider],
    ) -> None:
        self._session_factory = session_factory or get_session_factory()
        normalized = {}
        for name, provider in providers.items():
            key = _token(str(name), "CI provider", 120).lower()
            if key in normalized:
                raise ValueError(f"duplicate CI provider {key}")
            normalized[key] = provider
        self._providers = normalized

    async def begin_tracking(
        self,
        *,
        run_id: str,
        cycle_id: str,
        provider: str,
        repository: str,
        revision: str,
        external_run_id: str,
        check_id: str | None = None,
        url: str | None = None,
    ) -> FactoryCiRun:
        provider = _token(provider, "CI provider", 120).lower()
        repository = _token(repository, "CI repository")
        revision = _token(revision, "CI revision")
        external_run_id = _token(external_run_id, "CI run ID")
        check_id = _token(check_id, "CI check ID") if check_id is not None else ""
        if provider not in self._providers:
            raise FactoryCiError(
                "FACTORY_CI_PROVIDER_UNAVAILABLE",
                f"CI provider {provider} is not configured",
            )
        now = _now_ms()
        async with self._session_factory() as db:
            async with db.begin():
                run = await db.get(FactoryRun, run_id)
                cycle = await db.get(FactoryCycle, cycle_id)
                if run is None or cycle is None or cycle.run_id != run_id:
                    raise KeyError("factory run/cycle not found")
                if run.current_cycle_id != cycle_id:
                    raise FactoryCiError(
                        "FACTORY_CI_STALE_CYCLE", "CI tracking requires the current factory cycle"
                    )
                exact = (
                    await db.execute(
                        select(FactoryCiRun).where(
                            FactoryCiRun.provider == provider,
                            FactoryCiRun.repository == repository,
                            FactoryCiRun.external_run_id == external_run_id,
                            FactoryCiRun.check_id == check_id,
                        )
                    )
                ).scalar_one_or_none()
                if exact is not None:
                    if exact.run_id != run_id or exact.cycle_id != cycle_id or exact.revision != revision:
                        raise FactoryCiError(
                            "FACTORY_CI_TRACKING_CONFLICT",
                            "existing CI identity is bound to a different factory target",
                        )
                    return exact
                pending_diagnosis = (
                    await db.execute(
                        select(FactoryCiRun).where(
                            FactoryCiRun.cycle_id == cycle_id,
                            FactoryCiRun.provider == provider,
                            FactoryCiRun.revision == revision,
                            FactoryCiRun.diagnosis_required.is_(True),
                        )
                    )
                ).scalars().first()
                if pending_diagnosis is not None:
                    raise FactoryCiError(
                        "FACTORY_CI_DIAGNOSIS_REQUIRED",
                        "failed CI must be diagnosed before another run is tracked for this revision",
                    )
                row = FactoryCiRun(
                    run_id=run_id,
                    cycle_id=cycle_id,
                    provider=provider,
                    repository=repository,
                    revision=revision,
                    external_run_id=external_run_id,
                    check_id=check_id,
                    status="QUEUED",
                    conclusion=None,
                    url=redact_text(url)[:4_096] if url else None,
                    diagnosis_required=False,
                    created_at=now,
                    updated_at=now,
                )
                db.add(row)
            return row

    async def poll_once(self, ci_run_id: str) -> FactoryCiRun:
        row = await self._get(ci_run_id)
        provider = self._providers.get(row.provider)
        if provider is None:
            raise FactoryCiError(
                "FACTORY_CI_PROVIDER_UNAVAILABLE",
                f"CI provider {row.provider} is not configured",
            )
        # Exactly one provider observation per call. Scheduling/retries happen at
        # the durable orchestrator level; no sleep loop is hidden in this method.
        observation = await provider.observe(
            CiPollRequest(
                provider=row.provider,
                repository=row.repository,
                revision=row.revision,
                external_run_id=row.external_run_id,
                check_id=row.check_id,
            )
        )
        status = _status(observation.status)
        conclusion = _conclusion(observation.conclusion)
        if status != "COMPLETED" and conclusion is not None:
            raise FactoryCiError(
                "FACTORY_CI_INVALID_OBSERVATION",
                "CI conclusion cannot be terminal while status is non-terminal",
            )
        async with self._session_factory() as db:
            async with db.begin():
                current = await db.get(FactoryCiRun, ci_run_id)
                if current is None:
                    raise KeyError("factory CI run not found")
                latest_observed = (
                    await db.execute(
                        select(func.max(FactoryCiRun.last_observed_at)).where(
                            FactoryCiRun.cycle_id == current.cycle_id,
                            FactoryCiRun.provider == current.provider,
                            FactoryCiRun.revision == current.revision,
                        )
                    )
                ).scalar_one()
                now = max(_now_ms(), int(latest_observed or 0) + 1)
                current.status = status
                current.conclusion = conclusion
                if observation.url:
                    current.url = redact_text(observation.url)[:4_096]
                if observation.failure_summary:
                    current.failure_summary = redact_text(observation.failure_summary)[:8_000]
                current.diagnosis_required = bool(conclusion in _FAILURE_CONCLUSIONS)
                current.last_observed_at = now
                current.updated_at = now
            return current

    async def record_diagnosis(self, ci_run_id: str, summary: str) -> FactoryCiRun:
        summary = summary.strip()
        if not summary:
            raise ValueError("CI diagnosis summary must not be blank")
        now = _now_ms()
        async with self._session_factory() as db:
            async with db.begin():
                row = await db.get(FactoryCiRun, ci_run_id)
                if row is None:
                    raise KeyError("factory CI run not found")
                if row.conclusion not in _FAILURE_CONCLUSIONS:
                    raise FactoryCiError(
                        "FACTORY_CI_DIAGNOSIS_NOT_REQUIRED",
                        "only failed CI observations require rerun diagnosis",
                    )
                row.diagnosis_summary = redact_text(summary)[:8_000]
                row.diagnosis_required = False
                row.diagnosed_at = now
                row.updated_at = now
            return row

    async def has_current_pass(self, cycle_id: str, revision: str) -> bool:
        revision = _token(revision, "CI revision")
        async with self._session_factory() as db:
            latest = (
                await db.execute(
                    select(FactoryCiRun)
                    .where(
                        FactoryCiRun.cycle_id == cycle_id,
                        FactoryCiRun.revision == revision,
                        FactoryCiRun.last_observed_at.is_not(None),
                    )
                    .order_by(
                        FactoryCiRun.last_observed_at.desc(),
                        FactoryCiRun.updated_at.desc(),
                        FactoryCiRun.id.desc(),
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            return bool(
                latest is not None
                and latest.status == "COMPLETED"
                and latest.conclusion == "SUCCESS"
                and not latest.diagnosis_required
            )

    async def _get(self, ci_run_id: str) -> FactoryCiRun:
        async with self._session_factory() as db:
            row = await db.get(FactoryCiRun, ci_run_id)
            if row is None:
                raise KeyError("factory CI run not found")
            return row
